#!/usr/bin/env python3
"""Generative answer engine for the HUD.

Two independent triggers, deliberately kept in separate output streams:

  * a question detected across the rolling conversation -> answered with the
    higher-quality model and surfaced as a Q&A card;
  * a periodic (default 35 s) refresh -> short *new* talking points appended to
    a persistent bullet list (never mixed in with the Q&A cards).

Both are grounded in the recent conversation plus retrieved ``.md`` snippets.
All provider calls are governed by :class:`hud.budget.BudgetGovernor` so an
aggressive cadence degrades gracefully instead of hammering a free tier.
"""

from __future__ import annotations

import itertools
import json
import math
import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .budget import BudgetGovernor
from .config import HudConfig, get_provider
from .kb import KBIndex
from .llm import LLMClient, LLMError
from .memory import extract_memory, format_memory
from .state import LiveState, is_duplicate_point

QUESTION_WORDS = re.compile(
    r"^\s*(what|how|why|when|who|where|which|can|could|would|should|do|does|did|"
    r"is|are|was|were|will|may|might|tell me|explain|walk me|give me)\b",
    re.I,
)
QUESTION_PHRASES = re.compile(
    r"\b(tell me about|explain|what's|whats|how do|how does|how would|"
    r"can you|could you|would you|do you|did you|are you|is there|"
    r"any thoughts|your take|walk me through|walk us through)\b",
    re.I,
)
BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# Follow-up questions that only make sense with earlier context: short, open
# with a discourse connector, or lean on a pronoun. These are the ones worth a
# cheap rewrite pass before answering.
AMBIGUOUS_START_RE = re.compile(
    r"^\s*(what about|how about|and\b|so\b|then\b|why\b|which one|the other|both|those|these)\b",
    re.I,
)
REFERENCE_RE = re.compile(
    r"\b(it|that|this|those|these|they|them|he|she|one|ones|there|the other)\b",
    re.I,
)

# Backchannel / rhetorical questions that don't need an answer.
RHETORICAL_RE = re.compile(
    r"(?:,\s*(?:right|correct|yeah|okay|ok|no)\?|\bisn'?t (?:it|that|this)\b|"
    r"\baren'?t (?:they|we|you)\b|"
    r"\b(?:you know|make sense|sound good|does that make sense)\s*\?)\s*$",
    re.I,
)

# Speech-to-text often drops the final question mark from indirect questions.
# This remains a local high-recall signal; it does not trigger any new network
# call or attempt to answer until the normal answer gate accepts it.
INDIRECT_QUESTION_RE = re.compile(
    r"\b(?:i(?:'m| am| was) wondering|i(?:'d| would) like to know|"
    r"i(?:'m| am) curious|help me understand|i want to understand|"
    r"i(?:'m| am) trying to understand|could you tell (?:me|us)|"
    r"would you mind|do you happen to know|is there any chance|"
    r"can i ask|the question is)\b"
    r".{0,100}\b(?:whether|if|what|how|why|when|where|which|who)\b",
    re.I,
)


@dataclass
class Turn:
    seq: int
    ts: float
    speaker: str
    text: str
    finalized: bool = True
    speaker_id: str = ""


# --------------------------------------------------------------------------
# Pure text helpers (unit-tested)
# --------------------------------------------------------------------------
def split_sentences(text: str) -> List[str]:
    return [s.strip() for s in SENTENCE_RE.split((text or "").strip()) if s.strip()]


def is_question_sentence(sentence: str) -> bool:
    s = (sentence or "").strip().lstrip("\"'“”‘’")
    if not s:
        return False
    if s.endswith("?"):
        return True
    if QUESTION_WORDS.match(s):
        return True
    if QUESTION_PHRASES.search(s):
        return True
    if INDIRECT_QUESTION_RE.search(s):
        return True
    return False


def is_rhetorical_question(question: str) -> bool:
    return bool(RHETORICAL_RE.search((question or "").strip()))


def detect_question_text(text: str) -> Optional[str]:
    """Return the most recent question-like sentence in one blob of text."""
    candidate: Optional[str] = None
    for sentence in split_sentences(text):
        if is_question_sentence(sentence):
            candidate = sentence
    return candidate


# Backwards-compatible name for the single-string helper.
detect_question = detect_question_text


def _format_turn(turn: Turn) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(turn.ts))
    prefix = "{}: ".format(turn.speaker) if turn.speaker else ""
    return "[{}] {}{}".format(stamp, prefix, turn.text)


def _question_from_turn(turn: Turn) -> Optional[str]:
    found: Optional[str] = None
    for sentence in split_sentences(turn.text):
        if is_question_sentence(sentence):
            found = sentence
    return found


def _question_from_adjacent_turns(turns: List[Turn], index: int) -> Optional[str]:
    """Recover an indirect question split at an STT chunk boundary."""
    if index <= 0:
        return None
    prior = " ".join(t.text.strip() for t in turns[max(0, index - 2):index]
                      if t.text.strip())
    # If the preceding window already formed the question, this turn is not a
    # new question; avoid re-emitting the same indirect question as the rolling
    # lookback advances.
    if INDIRECT_QUESTION_RE.search(prior):
        return None
    joined = " ".join(t.text.strip() for t in turns[max(0, index - 2):index + 1]
                      if t.text.strip())
    question = detect_question_text(joined)
    if not question:
        return None
    if question == detect_question_text(prior):
        return None
    return question


def detect_questions_since(turns: List[Turn], after_seq: int,
                           lookback_seconds: float = 90.0,
                           now: Optional[float] = None) -> List[Dict[str, Any]]:
    """All question turns newer than ``after_seq`` inside the lookback window.

    Returns them oldest-first so the caller can answer a burst of questions in
    the order they were asked.
    """
    if not turns:
        return []
    now = time.time() if now is None else now
    cutoff = now - max(1.0, lookback_seconds)
    recent = [t for t in turns if t.ts >= cutoff]
    out: List[Dict[str, Any]] = []
    for idx, turn in enumerate(recent):
        if turn.seq <= after_seq:
            continue
        question = (_question_from_turn(turn)
                    or _question_from_adjacent_turns(recent, idx))
        if not question:
            continue
        context = "\n".join(_format_turn(t) for t in recent[max(0, idx - 2):idx])
        out.append({
            "question": question,
            "speaker": turn.speaker or "",
            "speaker_id": turn.speaker_id or "",
            "seq": turn.seq,
            "context": context,
            "finalized": bool(turn.finalized),
        })
    return out


def detect_question_turns(turns: List[Turn], lookback_seconds: float = 90.0,
                          now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Find the most recent question across the rolling conversation.

    Unlike the old single-chunk check this scans every turn inside the lookback
    window, so a question split across STT chunks (or asked a few turns ago) is
    still caught. It also returns the turns immediately preceding the question
    so the answer prompt can resolve references like "what about that one?".
    """
    found = detect_questions_since(turns, 0, lookback_seconds, now)
    return found[-1] if found else None


def is_ambiguous_question(question: str) -> bool:
    """True when a question likely needs earlier turns to be understood."""
    q = (question or "").strip()
    if not q:
        return False
    # Very short fragments still need context, but a six-word cutoff treats
    # perfectly self-contained interview questions as ambiguous. Keep the
    # conservative rule only for fragments of three words or fewer.
    if len(q.split()) <= 3:
        return True
    if AMBIGUOUS_START_RE.match(q):
        return True
    if REFERENCE_RE.search(q):
        return True
    return False


def parse_bullets(text: str) -> List[str]:
    """Parse bullets from a JSON object (preferred) or plain markdown."""
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            bullets = obj.get("bullets") or obj.get("points") or obj.get("claims") or []
            if isinstance(bullets, list):
                out = []
                for bullet in bullets:
                    value = (bullet.get("text") or bullet.get("claim") or bullet.get("point")
                             if isinstance(bullet, dict) else bullet)
                    if str(value).strip():
                        out.append(str(value).strip())
                return out
        except ValueError:
            pass
    bullets: List[str] = []
    for line in text.splitlines():
        m = BULLET_RE.match(line)
        if m:
            bullets.append(m.group(1).strip())
    if not bullets:
        # Fall back to sentences so we never show an empty card.
        bullets = [s.strip() for s in split_sentences(text) if s.strip()]
    return bullets[:6]


def parse_answer_claims(text: str) -> List[Dict[str, str]]:
    """Parse answer claims with optional exact evidence and source labels."""
    raw = (text or "").strip()
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            values = obj.get("claims") or obj.get("bullets") or obj.get("points") or []
            if isinstance(values, list):
                out = []
                for value in values:
                    if isinstance(value, dict):
                        claim = str(value.get("text") or value.get("claim") or
                                    value.get("point") or "").strip()
                        evidence = str(value.get("evidence") or value.get("quote") or "").strip()
                        source = str(value.get("source") or "").strip()
                    else:
                        claim, evidence, source = str(value).strip(), "", ""
                    if claim:
                        out.append({"text": claim, "evidence": evidence, "source": source})
                return out[:6]
        except (ValueError, TypeError, AttributeError):
            pass
    return [{"text": bullet, "evidence": "", "source": ""}
            for bullet in parse_bullets(raw)]


def verify_answer_claims(claims: List[Dict[str, str]], transcript: str,
                        snippets: List[Any]) -> Tuple[List[str], List[Dict[str, str]], List[str]]:
    """Return display text, verified evidence records, and dropped claims.

    Explicit evidence is strict. Legacy/plain-text model output remains
    displayable for provider compatibility, but gets no ``verified`` record;
    this makes the UI and saved artifact distinguish audited claims from a
    provider that ignored the requested schema.
    """
    reference = transcript or ""
    source_names = set()
    for snippet in snippets:
        source = str(getattr(snippet, "source", "") or "")
        if source:
            source_names.add(source)
        reference += "\n" + str(getattr(snippet, "text", "") or "")
    display: List[str] = []
    evidence: List[Dict[str, str]] = []
    dropped: List[str] = []
    for claim in claims:
        text = str(claim.get("text") or "").strip()
        quote = str(claim.get("evidence") or "").strip()
        source = str(claim.get("source") or "").strip()
        if not text:
            continue
        if quote and not grounded_in(text, quote, reference, min_tokens=2, threshold=0.65):
            dropped.append(text)
            continue
        if source and source not in source_names:
            dropped.append(text)
            continue
        display.append(text)
        if quote:
            evidence.append({"claim": text, "evidence": quote, "source": source})
    return display[:6], evidence[:6], dropped[:6]


_PUNCT_RE = re.compile(r"[^a-z0-9\s]")


def _normalize_tokens(text: str) -> List[str]:
    return _PUNCT_RE.sub(" ", (text or "").lower()).split()


def parse_point_objects(text: str) -> List[Dict[str, str]]:
    """Parse transcript-grounded points ``{"text", "quote"}``.

    Prefers the JSON schema (objects with an evidence quote); tolerates plain
    string bullets and markdown fallbacks so a model that ignores the schema
    still produces *something* we can ground-check.
    """
    text = (text or "").strip()
    out: List[Dict[str, str]] = []
    if not text:
        return out
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            bullets = obj.get("bullets") or obj.get("points") or []
            if isinstance(bullets, list):
                for item in bullets:
                    if isinstance(item, dict):
                        point = str(item.get("text") or item.get("point") or "").strip()
                        quote = str(item.get("quote") or item.get("evidence") or "").strip()
                        if point:
                            out.append({"text": point, "quote": quote})
                    elif str(item).strip():
                        out.append({"text": str(item).strip(), "quote": ""})
                return out
        except ValueError:
            pass
    for line in text.splitlines():
        m = BULLET_RE.match(line)
        if m and m.group(1).strip():
            out.append({"text": m.group(1).strip(), "quote": ""})
    if not out:
        out = [{"text": s, "quote": ""} for s in split_sentences(text) if s.strip()]
    return out


def grounded_in(point_text: str, quote: str, window: str,
                min_tokens: int = 4, threshold: float = 0.7) -> bool:
    """True when a point is actually supported by the transcript window.

    A supplied quote must match the window (exact normalised span, or token
    overlap above ``threshold``). With no quote, the bullet's own tokens must
    overlap the window. This is the cheap guard that stops the model inventing
    plausible-but-unsaid content.
    """
    window_tokens = _normalize_tokens(window)
    if not window_tokens:
        return False
    window_text = " ".join(window_tokens)
    window_set = set(window_tokens)

    def matches(candidate: str) -> bool:
        tokens = _normalize_tokens(candidate)
        if len(tokens) < min_tokens:
            return False
        if " ".join(tokens) in window_text:
            return True
        overlap = sum(1 for t in tokens if t in window_set) / len(tokens)
        return overlap >= threshold

    if quote.strip():
        return matches(quote)
    return matches(point_text)


def estimate_tokens(text: str, max_tokens: int) -> int:
    return max(1, len(text) // 4) + max_tokens


def parse_follow_up(text: str) -> bool:
    """True when the model marked an answer as a follow-up to the previous one."""
    text = (text or "").strip()
    if text.startswith("{"):
        try:
            return bool(json.loads(text).get("follow_up"))
        except ValueError:
            return False
    return False


def parse_summary(text: str) -> Dict[str, Any]:
    """Parse the end-of-call JSON, tolerating a plain-text fallback."""
    text = (text or "").strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    return {"summary": text, "action_items": [], "follow_up_email": ""}


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class _Chain:
    """Ordered provider chain with model selection per role."""

    def __init__(self, entries: List[Dict[str, Any]]) -> None:
        self.entries = entries

    def __bool__(self) -> bool:
        return bool(self.entries)


class AnswerEngine:
    def __init__(self, state: LiveState, log: Callable[[str], None],
                 cfg: HudConfig, budget: Optional[BudgetGovernor] = None,
                 outdir: Optional[Any] = None) -> None:
        self.state = state
        self.log = log
        self.cfg = cfg
        self.budget = budget or BudgetGovernor()
        self.outdir = Path(outdir) if outdir else None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._rolling_worker: Optional[threading.Thread] = None
        self._queue: "queue.PriorityQueue" = queue.PriorityQueue()
        self._rolling_queue: "queue.Queue" = queue.Queue(maxsize=1)
        self._job_seq = itertools.count()
        self._provider_gate = threading.Lock()
        self._paused = False

        self._id_cursor = 0
        self._seq = 0
        self._buffer: List[Turn] = []
        self._pending_turns: List[Turn] = []
        self._last_question_seq = 0
        self._last_answer_id = 0
        self._qa_thread: List[Dict[str, Any]] = []
        self._rolling_payload: Optional[Dict[str, Any]] = None
        self._rolling_queued = False
        self._last_rolling_ts = 0.0
        self._last_rolling_hash = ""
        self._words_since_rolling = 0
        self._question_inflight = False
        self._partial_question_draft = ""

        self._kb: Optional[KBIndex] = None
        self._live_kb: Optional[KBIndex] = None
        self._live_q: "queue.Queue" = queue.Queue()
        self._live_kb_thread: Optional[threading.Thread] = None
        self._memory_q: "queue.Queue" = queue.Queue(maxsize=256)
        self._memory_thread: Optional[threading.Thread] = None
        self._embedder: Any = None
        self._vec_cache: Dict[str, List[float]] = {}
        self._chain: Optional[_Chain] = None
        self._chain_index = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hud-answers", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put((0, next(self._job_seq), None))
        try:
            self._rolling_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._live_kb_thread is not None:
            try:
                self._live_q.put_nowait(None)
            except queue.Full:
                pass
        if self._memory_thread is not None:
            try:
                self._memory_q.put_nowait(None)
            except queue.Full:
                pass
        if self._thread is not None:
            self._thread.join(timeout=12.0)
        if self._live_kb_thread is not None:
            self._live_kb_thread.join(timeout=5.0)
        if self._memory_thread is not None:
            self._memory_thread.join(timeout=3.0)
        if self._chain is not None:
            for entry in self._chain.entries:
                closer = getattr(entry.get("client"), "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001
                        pass

    def pause(self, paused: bool = True) -> None:
        with self._lock:
            self._paused = paused

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def ask(self, text: str, expand: bool = False) -> bool:
        """Queue a manual question (or 'expand this point') from the UI."""
        text = (text or "").strip()
        if not text or self.paused:
            return False
        question = ("Expand on this talking point with concrete detail, an example, "
                    "and why it matters: {}".format(text)) if expand else text
        self._put(0, {"kind": "question", "question": question, "speaker": "You",
                      "context": "", "window": self._context_text(), "manual": True})
        return True

    # -- setup -------------------------------------------------------------
    def _build_chain(self) -> _Chain:
        names: List[str] = [self.cfg.answers_backend] + list(self.cfg.answers_fallback)
        entries: List[Dict[str, Any]] = []
        seen = set()
        for name in names:
            key = (name or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            try:
                provider = get_provider(key)
            except KeyError:
                self.log("answers: unknown provider '{}' skipped".format(name))
                continue
            api_key = self.cfg.api_key_for(provider.name)
            if provider.api_key_env and not api_key:
                self.log("answers: '{}' skipped (no {})".format(provider.name, provider.api_key_env))
                continue
            entries.append({
                "name": provider.name,
                "client": LLMClient(provider.base_url, api_key),
                "chat_model": self.cfg.chat_model or provider.chat_model,
                "rolling_model": self.cfg.rolling_model or provider.rolling_model,
                "structured": provider.name in ("groq", "openai", "openrouter"),
            })
        return _Chain(entries)

    def _start_kb(self) -> None:
        """Build the embedder/KB on a side thread so answers aren't blocked.

        The embedder is also used for semantic talking-point dedupe and the
        live transcript index, so it is built even when the static KB is
        disabled. The live transcript index (self._live_kb) grows as turns
        arrive and makes earlier conversation retrievable beyond the 5-minute
        context window.
        """
        want_kb = self.cfg.kb_enabled and bool(self.cfg.kb_dirs)
        want_dedupe = self.cfg.point_dedupe_score > 0
        # Always build the embedder so the live transcript index works even
        # when no static KB dirs are configured.
        if not (want_kb or want_dedupe):
            want_dedupe = True

        def _work() -> None:
            try:
                embedder = self._build_embedder()
                if embedder is None:
                    if want_kb:
                        self.log("KB: no embedding backend available; continuing without KB")
                    return
                self._embedder = embedder

                # Live transcript index: starts empty, grows with the call.
                # A lower min_score so marginally relevant older turns are
                # still retrievable. Capped to bound memory on long calls.
                self._live_kb = KBIndex([], embedder, log=self.log, min_score=0.05,
                                        max_chunks=2000)
                self._live_kb.init_empty()
                self._live_kb_thread = threading.Thread(
                    target=self._live_kb_loop, name="hud-live-kb", daemon=True)
                self._live_kb_thread.start()
                self.log("live transcript index ready ({})".format(
                    getattr(embedder, "label", "?")))

                if want_kb:
                    # Auto-include the recordings base directory so past call
                    # transcripts and summaries become searchable reference
                    # material alongside any user-configured dirs.
                    dirs = list(self.cfg.kb_dirs)
                    if self.outdir is not None and self.outdir.parent:
                        parent = self.outdir.parent
                        if parent.is_dir() and str(parent) not in dirs:
                            dirs.append(str(parent))
                            self.log("KB: auto-included recordings dir {}".format(parent))
                    kb = KBIndex(dirs, embedder,
                                 cache_dir=self.cfg.kb_cache_dir, log=self.log,
                                 min_score=self.cfg.kb_min_score)
                    if kb.build(force=self.cfg.kb_reindex):
                        self._kb = kb
                    else:
                        self.log("KB: disabled (build failed or no files)")
            except Exception as exc:  # noqa: BLE001
                self.log("KB setup failed: {}".format(exc))

        threading.Thread(target=_work, name="hud-kb", daemon=True).start()

    def _live_kb_loop(self) -> None:
        """Background thread: batch-embed transcript turns into the live KB.

        Keeping this off the main answer loop means a slow remote embedder
        never blocks question detection or the event-driven answer loop.
        """
        batch: List[Dict[str, str]] = []
        while not self._stop.is_set():
            try:
                item = self._live_q.get(timeout=1.0)
            except queue.Empty:
                if batch and self._live_kb is not None:
                    self._live_kb.add_chunks(batch)
                    batch = []
                continue
            if item is None:
                break
            batch.append(item)
            if len(batch) >= 8:
                if self._live_kb is not None:
                    self._live_kb.add_chunks(batch)
                batch = []
        if batch and self._live_kb is not None:
            self._live_kb.add_chunks(batch)

    def _memory_loop(self) -> None:
        """Extract deterministic meeting facts off the answer loop."""
        while not self._stop.is_set():
            try:
                event = self._memory_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if event is None:
                self._memory_q.task_done()
                break
            try:
                for item in extract_memory(
                        str(event.get("text") or ""),
                        str(event.get("speaker") or ""),
                        float(event.get("ts") or time.time()),
                        int(event.get("id") or 0)):
                    self.state.add_memory_item(item)
            finally:
                self._memory_q.task_done()

    def _build_embedder(self):
        """Pick an embedding backend: local model, Ollama, OpenAI, then hashing."""
        from .kb import HashingEmbedder, LocalEmbedder, RemoteEmbedder

        backend = (self.cfg.kb_embed_backend or "auto").lower()
        model = self.cfg.kb_embed_model
        if backend in ("local", "sentence-transformers", "auto"):
            try:
                return LocalEmbedder(model or self.cfg.kb_model)
            except Exception as exc:  # noqa: BLE001
                self.log("KB: local embeddings unavailable ({})".format(exc))
                if backend != "auto":
                    return None
        if backend in ("ollama", "auto"):
            try:
                provider = get_provider("ollama")
                return RemoteEmbedder(
                    LLMClient(provider.base_url, None),
                    model or "nomic-embed-text", name="ollama")
            except Exception as exc:  # noqa: BLE001
                self.log("KB: ollama embeddings unavailable ({})".format(exc))
        if backend in ("openai", "auto"):
            api_key = self.cfg.api_key_for("openai")
            if api_key:
                provider = get_provider("openai")
                return RemoteEmbedder(
                    LLMClient(provider.base_url, api_key),
                    model or "text-embedding-3-small", name="openai")
        # Final fallback: zero-dependency hashing embedder. Always works,
        # so the live transcript index and semantic dedup never go dark.
        if backend in ("auto", "hashing"):
            self.log("KB: using hashing embedder (no neural backend available)")
            return HashingEmbedder()
        return None

    # -- main loop ---------------------------------------------------------
    def _run(self) -> None:
        self._chain = self._build_chain()
        enabled = self.cfg.answers_enabled and bool(self._chain)
        self.state.set_status("recording", answers_enabled=enabled,
                              answers_backend=self.cfg.answers_backend,
                              answers_error="" if enabled else "no provider/key configured")
        if not enabled:
            self.log("answers disabled: {}".format(
                "feature off" if not self.cfg.answers_enabled else "no provider key"))
            return

        self._id_cursor = self.state.latest_id()
        self.state.set_budget(self.budget.snapshot())
        self._start_kb()
        self._memory_thread = threading.Thread(target=self._memory_loop,
                                               name="hud-memory", daemon=True)
        self._memory_thread.start()
        self._worker = threading.Thread(target=self._worker_loop,
                                        name="hud-answers-work", daemon=True)
        self._worker.start()
        self._rolling_worker = threading.Thread(target=self._rolling_worker_loop,
                                                name="hud-talking-points", daemon=True)
        self._rolling_worker.start()
        self.log("answers enabled via {} (chat={}, rolling={}, interval={:.0f}s)".format(
            self.cfg.answers_backend, self._chain.entries[0]["chat_model"],
            self._chain.entries[0]["rolling_model"], self.cfg.answer_interval))

        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self.log("answers loop error: {}".format(exc))
            if self._stop.is_set():
                break
            # Transcript events wake the loop immediately; the timeout keeps
            # budget timers and rolling-point scheduling alive during silence.
            # This removes the old fixed one-second question-detection delay.
            self.state.wait_for(self._id_cursor, timeout=1.0)

        if self._worker is not None:
            self._worker.join(timeout=12.0)
        if self._rolling_worker is not None:
            self._rolling_worker.join(timeout=3.0)
        self._flush_buffer()

    def _put(self, priority: int, job: Dict[str, Any]) -> None:
        job.setdefault("queued_at", time.time())
        job.setdefault("trace_id", "answer-{}".format(secrets.token_hex(6)))
        self._trace(job["trace_id"], "queued", kind=job.get("kind", "question"),
                    queued_at=job["queued_at"])
        self._queue.put((priority, next(self._job_seq), job))

    def _put_rolling(self, job: Dict[str, Any]) -> None:
        job.setdefault("queued_at", time.time())
        job.setdefault("trace_id", "talking-points-{}".format(secrets.token_hex(6)))
        self._trace(job["trace_id"], "queued", kind="rolling",
                    queued_at=job["queued_at"])
        try:
            self._rolling_queue.put_nowait(job)
        except queue.Full:
            try:
                self._rolling_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._rolling_queue.put_nowait(job)
            except queue.Full:
                pass

    def _trace(self, trace_id: str, stage: str, **fields: Any) -> None:
        """Record a redacted, stage-level answer/talking-point trace."""
        payload = {"trace_id": str(trace_id), "stage": str(stage)}
        payload.update(fields)
        self.state.add("answer_trace", **payload)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                _priority, _seq, job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if job is None:
                break
            if self.paused:
                continue
            try:
                self._execute(job)
            except Exception as exc:  # noqa: BLE001
                self.log("answers job error: {}".format(exc))

    def _rolling_worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._rolling_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if job is None:
                break
            if self.paused:
                continue
            try:
                self._execute(job)
            except Exception as exc:  # noqa: BLE001
                self.log("talking-point job error: {}".format(exc))

    def _execute(self, job: Dict[str, Any]) -> None:
        if job.get("kind") == "rolling":
            with self._lock:
                self._rolling_queued = False
                payload = self._rolling_payload
                self._rolling_payload = None
            if payload:
                self._refresh_talking_points(payload)
            return
        with self._lock:
            self._question_inflight = True
        try:
            self._answer_question(job)
        finally:
            with self._lock:
                self._question_inflight = False

    def _tick(self) -> None:
        self._drain_events()
        self._trim_buffer()
        self.state.set_budget(self.budget.snapshot())

        if self.paused or self.budget.blocked_seconds() > 0:
            return
        now = time.time()

        # Enqueue every new question (oldest first); the worker answers them
        # with the stronger model without blocking detection.
        lookback = self.cfg.question_lookback_seconds
        for detected in detect_questions_since(self._buffer, self._last_question_seq,
                                               lookback, now):
            if not detected.get("finalized"):
                # A punctuation mark in an interim Whisper hypothesis is not
                # an endpoint. Wait for the explicit VAD/STT boundary so the
                # answer is built from the complete question.
                continue
            self._last_question_seq = max(self._last_question_seq, detected["seq"])
            if not self._should_answer(detected):
                continue
            self._put(0, {
                "kind": "question",
                "question": detected["question"],
                "speaker": detected["speaker"],
                "context": detected["context"],
                "window": self._context_text(),
                "detected_at": time.time(),
            })
            self._partial_question_draft = ""
            self.state.set_meta(question_draft="")

        if not self.cfg.rolling_enabled:
            return
        if now - self._last_rolling_ts < self.cfg.answer_interval:
            return
        window = self._context_text()
        if not window:
            self._last_rolling_ts = now
            return
        # Substance gate: don't manufacture points from sparse/noisy audio.
        if len(window.split()) < self.cfg.talking_points_min_words:
            self._last_rolling_ts = now
            return
        if self._words_since_rolling < self.cfg.talking_points_min_new_words:
            self._last_rolling_ts = now
            return
        with self._lock:
            question_busy = self._question_inflight
        with self._queue.mutex:
            pending_question = any(
                item[2] is not None and item[2].get("kind") == "question"
                for item in list(self._queue.queue))
        if question_busy or pending_question:
            # Talking points are deliberately best effort. Do not add another
            # low-priority call while a question is waiting or being answered.
            return
        digest = str(hash(window))
        if digest == self._last_rolling_hash:
            self._last_rolling_ts = now
            return
        if self.budget.daily_budget_low():
            # Degrade talking points first; keep question answers working.
            self._last_rolling_ts = now
            return
        self._last_rolling_ts = now
        self._last_rolling_hash = digest
        self._words_since_rolling = 0
        with self._lock:
            trace_id = "talking-points-{}".format(secrets.token_hex(6))
            self._rolling_payload = {"window": window, "queued_at": time.time(),
                                     "trace_id": trace_id}
            if not self._rolling_queued:
                self._rolling_queued = True
                self._put_rolling({"kind": "rolling", "trace_id": trace_id})

    def _should_answer(self, detected: Dict[str, Any]) -> bool:
        question = detected.get("question", "")
        speaker = detected.get("speaker") or ""
        speaker_id = detected.get("speaker_id") or ""
        if is_rhetorical_question(question):
            return False
        if (not self.cfg.answer_self_questions and
                (speaker_id == "local" or (speaker and speaker == self.cfg.self_name))):
            return False
        return True

    # -- transcript buffer -------------------------------------------------
    def _update_partial_question_draft(self, text: str, speaker: str,
                                       speaker_id: str) -> None:
        """Use provisional words for UI question drafting, never evidence."""
        draft = str(text or "").strip()
        if not draft:
            return
        prior = ""
        for turn in reversed(self._pending_turns):
            if ((speaker_id and turn.speaker_id == speaker_id) or
                    (not speaker_id and turn.speaker == speaker)):
                prior = turn.text
                break
        if not prior:
            for turn in reversed(self._buffer):
                if ((speaker_id and turn.speaker_id == speaker_id) or
                        (not speaker_id and turn.speaker == speaker)):
                    prior = turn.text
                    break
        candidate = (prior + " " + draft).strip()
        question = detect_question_text(candidate)
        if question and question != self._partial_question_draft:
            self._partial_question_draft = question
            self.state.set_meta(question_draft=question)

    def _drain_events(self) -> None:
        events = self.state.since(self._id_cursor)
        for event in events:
            self._id_cursor = max(self._id_cursor, int(event.get("id", 0)))
            if event.get("type") == "transcript_partial":
                # This is a UI-only draft. It can help the user see a question
                # forming, but it must never enter the authoritative buffer or
                # an answer prompt until stable words arrive as transcript events.
                speaker = event.get("speaker") or ""
                speaker_id = event.get("speaker_id") or ""
                self._update_partial_question_draft(
                    str(event.get("text") or ""), speaker, speaker_id)
                continue
            if event.get("type") == "transcript_boundary":
                speaker = event.get("speaker") or ""
                speaker_id = event.get("speaker_id") or ""
                matching = [turn for turn in self._pending_turns
                            if ((speaker_id and turn.speaker_id == speaker_id) or
                                (not speaker_id and turn.speaker == speaker))]
                self._pending_turns = [turn for turn in self._pending_turns
                                       if turn not in matching]
                if not event.get("finalized", False):
                    for turn in matching:
                        turn.finalized = True
                    self._buffer.extend(matching)
                continue
            if event.get("type") == "transcript" and event.get("source") == "live":
                text = str(event.get("text", "")).strip()
                if not text:
                    continue
                self._seq += 1
                speaker = event.get("speaker") or ""
                speaker_id = event.get("speaker_id") or ""
                turn = Turn(
                    seq=self._seq, ts=float(event.get("ts", time.time())),
                    speaker=speaker, text=text,
                    finalized=bool(event.get("finalized", True)),
                    speaker_id=speaker_id)
                if not turn.finalized:
                    self._pending_turns.append(turn)
                    self._update_partial_question_draft(text, speaker, speaker_id)
                    continue
                self._buffer.append(turn)
                self._words_since_rolling += len(text.split())
                # Feed each turn into the live transcript index so older
                # conversation is retrievable by meaning, not just the
                # rolling 5-minute window.
                if self._live_kb is not None:
                    try:
                        self._live_q.put_nowait({
                            "source": "live_transcript",
                            "heading": speaker or "speaker",
                            "text": text,
                        })
                    except queue.Full:
                        pass
                try:
                    self._memory_q.put_nowait(dict(event))
                except queue.Full:
                    pass

    def _trim_buffer(self) -> None:
        cutoff = time.time() - self.cfg.context_minutes * 60.0
        self._buffer = [t for t in self._buffer if t.ts >= cutoff][-400:]
        self._pending_turns = [t for t in self._pending_turns if t.ts >= cutoff][-100:]

    def _context_text(self) -> str:
        text = "\n".join(_format_turn(t) for t in self._buffer).strip()
        limit = self.cfg.context_max_chars
        if limit and len(text) > limit:
            return text[-limit:]
        return text

    def _flush_buffer(self) -> None:
        self._buffer = []

    def _query_all(self, query_text: str, top_k: int) -> List:
        """Query the static KB and the live transcript index, merge by score.

        Both indexes share the same embedder, so cosine scores are directly
        comparable. The live index lets answers reference conversation from
        beyond the 5-minute context window; the static KB contributes
        user-provided reference docs and past recording transcripts.
        """
        from .kb import KBSnippet
        results: List[KBSnippet] = []
        if self._kb is not None:
            results.extend(self._kb.query(query_text, top_k))
        if self._live_kb is not None:
            results.extend(self._live_kb.query(query_text, top_k))
        results.sort(key=lambda s: s.score, reverse=True)
        selected = []
        remaining = max(0, int(getattr(self.cfg, "kb_max_chars", 6000)))
        for snippet in results[:top_k]:
            if remaining == 0:
                break
            text = str(getattr(snippet, "text", "") or "")
            if remaining > 0 and len(text) > remaining:
                text = text[:remaining]
            if not text:
                continue
            selected.append(KBSnippet(source=snippet.source,
                                      heading=snippet.heading,
                                      text=text, score=snippet.score))
            remaining -= len(text)
        return selected

    # -- generation --------------------------------------------------------
    def _qa_recent(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(qa) for qa in self._qa_thread]

    def _chat(self, kind: str, system: str, prompt: str,
              max_tokens: Optional[int] = None, temperature: float = 0.2,
              model_override: Optional[str] = None):
        # Questions get the provider lane; stale talking-point refreshes are
        # dropped when that lane is occupied instead of delaying an answer.
        acquired = self._provider_gate.acquire(blocking=(kind != "rolling"))
        if not acquired:
            self.state.set_meta(talking_point_dropped_busy=True)
            return None
        try:
            return self._chat_unlocked(kind, system, prompt, max_tokens,
                                       temperature, model_override)
        finally:
            self._provider_gate.release()

    def _chat_unlocked(self, kind: str, system: str, prompt: str,
              max_tokens: Optional[int] = None, temperature: float = 0.2,
              model_override: Optional[str] = None):
        """Run one provider call (with fallback + JSON-mode retry)."""
        if self._chain is None or not self._chain.entries:
            return None
        max_out = max_tokens or self.cfg.answer_max_tokens
        est = estimate_tokens(system + prompt, max_out)
        entries = self._chain.entries
        rate_limited_after: Optional[float] = None
        for offset in range(len(entries)):
            entry = entries[(self._chain_index + offset) % len(entries)]
            model = model_override or (
                entry["rolling_model"] if kind == "rolling" else entry["chat_model"])
            if not self.budget.can_afford(est):
                return None
            self.budget.reserve(est)
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]
            response_format = {"type": "json_object"} if entry.get("structured") else None
            try:
                try:
                    result = entry["client"].chat(
                        messages, model, max_tokens=max_out,
                        temperature=temperature, response_format=response_format)
                except LLMError as exc:
                    if exc.status == 400 and response_format is not None:
                        # Reasoning models (e.g. Groq's gpt-oss-120b) can emit
                        # truncated/invalid JSON under strict json mode, which
                        # Groq rejects with a 400. Retry as plain text; the
                        # bullet parser handles JSON-in-prose or plain bullets.
                        self.log("answers: {} JSON mode rejected; retrying plain text".format(
                            entry["name"]))
                        result = entry["client"].chat(
                            messages, model, max_tokens=max_out,
                            temperature=temperature, response_format=None)
                    else:
                        raise
            except LLMError as exc:
                self.budget.record(None, None)
                if exc.status == 429:
                    rate_limited_after = max(rate_limited_after or 0.0,
                                             exc.retry_after or 0.0)
                    self.log("answers: rate limited by {}; trying fallback".format(
                        entry["name"]))
                    continue
                self.log("answers: {} error: {}".format(entry["name"], exc))
                continue
            except Exception as exc:  # noqa: BLE001
                self.log("answers: {} unexpected error: {}".format(entry["name"], exc))
                continue
            self.budget.record(result.headers, result.usage)
            self._chain_index = (self._chain_index + offset) % len(entries)
            self.state.set_budget(self.budget.snapshot())
            return result
        if rate_limited_after is not None:
            self.budget.pause(rate_limited_after,
                              reason="all answer providers rate limited")
        return None

    def _chat_stream(self, kind: str, system: str, prompt: str,
                    max_tokens: Optional[int] = None, temperature: float = 0.2,
                    model_override: Optional[str] = None,
                    on_chunk: Optional[Callable[[str], None]] = None):
        acquired = self._provider_gate.acquire(blocking=(kind != "rolling"))
        if not acquired:
            self.state.set_meta(talking_point_dropped_busy=True)
            return None
        try:
            return self._chat_stream_unlocked(kind, system, prompt, max_tokens,
                                              temperature, model_override, on_chunk)
        finally:
            self._provider_gate.release()

    def _chat_stream_unlocked(self, kind: str, system: str, prompt: str,
                    max_tokens: Optional[int] = None, temperature: float = 0.2,
                    model_override: Optional[str] = None,
                    on_chunk: Optional[Callable[[str], None]] = None):
        """Streaming variant of _chat: calls chat_stream with on_chunk."""
        if self._chain is None or not self._chain.entries:
            return None
        max_out = max_tokens or self.cfg.answer_max_tokens
        est = estimate_tokens(system + prompt, max_out)
        entries = self._chain.entries
        rate_limited_after: Optional[float] = None
        for offset in range(len(entries)):
            entry = entries[(self._chain_index + offset) % len(entries)]
            model = model_override or (
                entry["rolling_model"] if kind == "rolling" else entry["chat_model"])
            if not self.budget.can_afford(est):
                return None
            self.budget.reserve(est)
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]
            response_format = {"type": "json_object"} if entry.get("structured") else None
            try:
                result = entry["client"].chat_stream(
                    messages, model, max_tokens=max_out,
                    temperature=temperature, response_format=response_format,
                    on_chunk=on_chunk)
            except LLMError as exc:
                if exc.status == 400 and response_format is not None:
                    # Retry plain text; the bullet parser handles JSON-in-prose.
                    self.log("answers: {} stream JSON mode rejected; retrying plain text".format(
                        entry["name"]))
                    try:
                        result = entry["client"].chat_stream(
                            messages, model, max_tokens=max_out,
                            temperature=temperature, on_chunk=on_chunk)
                    except LLMError as exc2:
                        self.budget.record(None, None)
                        if exc2.status == 429:
                            rate_limited_after = max(rate_limited_after or 0.0,
                                                     exc2.retry_after or 0.0)
                            self.log("answers: rate limited by {}; trying fallback".format(
                                entry["name"]))
                            continue
                        self.log("answers: {} stream error: {}".format(entry["name"], exc2))
                        continue
                else:
                    self.budget.record(None, None)
                    if exc.status == 429:
                        rate_limited_after = max(rate_limited_after or 0.0,
                                                 exc.retry_after or 0.0)
                        self.log("answers: rate limited by {}; trying fallback".format(
                            entry["name"]))
                        continue
                    self.log("answers: {} stream error: {}".format(entry["name"], exc))
                    continue
            except Exception as exc:  # noqa: BLE001
                self.log("answers: {} stream unexpected error: {}".format(entry["name"], exc))
                continue
            self.budget.record(result.headers, result.usage)
            self._chain_index = (self._chain_index + offset) % len(entries)
            self.state.set_budget(self.budget.snapshot())
            return result
        if rate_limited_after is not None:
            self.budget.pause(rate_limited_after,
                              reason="all answer providers rate limited")
        return None

    def _answer_question(self, job: Dict[str, Any]) -> None:
        detected_at = float(job.get("detected_at") or time.time())
        queued_at = float(job.get("queued_at") or detected_at)
        started_at = time.time()
        trace_id = str(job.get("trace_id") or "answer-{}".format(secrets.token_hex(6)))
        self._trace(trace_id, "started", detected_at=detected_at,
                    queued_at=queued_at, started_at=started_at)
        question = job.get("question") or ""
        context = job.get("context") or ""
        window = job.get("window") or self._context_text()
        if not question and not window:
            return

        assembly_started = time.time()
        rewritten = ""
        # Skip the rewrite LLM call when we have enough context for the
        # answer model to resolve references directly — the prompt already
        # includes preceding turns and instructs reference resolution.
        # Only rewrite very short, ambiguous questions with no context.
        if (not job.get("manual") and question and self.cfg.question_rewrite
                and is_ambiguous_question(question)
                and len(question.split()) <= 4
                and not context.strip()):
            rewritten = self._rewrite_question(question, context)
        effective = rewritten or question

        snippets = []
        if effective:
            query_text = "{} {}".format(effective, context or window[-1200:]).strip()
        else:
            query_text = window[-1500:]
        snippets = self._query_all(query_text, self.cfg.kb_top_k)

        system = self._system_prompt("question")
        prompt = self._build_prompt("question", effective, context, window, snippets,
                                    qa=self._qa_recent())
        assembly_seconds = round(time.time() - assembly_started, 3)
        queue_wait = max(0.0, started_at - queued_at)
        prompt_chars = len(system) + len(prompt)
        prompt_tokens = estimate_tokens(system + prompt, 0)
        reference_chars = sum(len(getattr(s, "text", "") or "") for s in snippets)
        self.state.observe_metric("answer_queue_wait", queue_wait)
        self.state.observe_metric("prompt_assembly_seconds", assembly_seconds)
        self.state.observe_metric("prompt_chars", prompt_chars)
        self._trace(trace_id, "assembled", queue_wait_seconds=round(queue_wait, 4),
                    assembly_seconds=assembly_seconds, prompt_chars=prompt_chars,
                    prompt_estimated_tokens=prompt_tokens,
                    context_chars=len(window), reference_chars=reference_chars)

        # Create a placeholder answer event so the UI shows the question is
        # being answered immediately, then stream the raw text in.
        placeholder = self.state.add(
            "answer", kind="question", question=question,
            rewritten_question=rewritten or None, bullets=[],
            sources=[s.source for s in snippets], model="", streaming=True,
            trace_id=trace_id,
            answer_queue_wait=round(queue_wait, 2),
            prompt_assembly_seconds=assembly_seconds,
            prompt_chars=prompt_chars,
            prompt_estimated_tokens=prompt_tokens,
            context_chars=len(window),
            reference_chars=reference_chars)
        event_id = placeholder["id"]
        accumulated = []

        def _on_chunk(delta: str) -> None:
            accumulated.append(delta)
            self.state.update_answer(event_id, streaming_text="".join(accumulated))

        result = self._chat_stream("question", system, prompt, on_chunk=_on_chunk)
        if result is None:
            total_seconds = max(0.0, time.time() - detected_at)
            self.state.observe_metric("answer_total_seconds", total_seconds)
            self._trace(trace_id, "failed", reason="all providers failed",
                        total_seconds=round(total_seconds, 4))
            self.state.update_answer(event_id, bullets=[], model="",
                                      streaming=False, error="all providers failed",
                                      provider_ttft=None, provider_seconds=None,
                                      trace_id=trace_id,
                                      answer_latency=round(total_seconds, 2))
            return

        provider_finished_at = time.time()
        total_seconds = max(0.0, provider_finished_at - detected_at)
        self._trace(trace_id, "provider_complete",
                    provider_ttft_seconds=result.ttft_seconds,
                    provider_request_seconds=result.request_seconds,
                    total_seconds=round(total_seconds, 4))

        claims = parse_answer_claims(result.text)
        bullets, evidence, dropped_claims = verify_answer_claims(
            claims, "{}\n{}".format(window, context), snippets)
        if not bullets:
            self.state.observe_metric("answer_total_seconds", total_seconds)
            self.state.update_answer(event_id, bullets=[], model=result.model,
                                      streaming=False, error="no answer produced",
                                      evidence=evidence, dropped_claims=dropped_claims,
                                      grounding="unverified" if dropped_claims else "none",
                                      trace_id=trace_id,
                                      provider_ttft=result.ttft_seconds,
                                      provider_seconds=result.request_seconds,
                                      answer_latency=round(total_seconds, 2))
            return

        parent_id = None
        if parse_follow_up(result.text):
            with self._lock:
                parent_id = self._last_answer_id or None
        self.state.observe_metric("answer_total_seconds", total_seconds)
        self.state.update_answer(
            event_id, bullets=bullets, model=result.model,
            usage=result.usage, parent_id=parent_id, streaming=False,
            streaming_text=None,
            evidence=evidence,
            dropped_claims=dropped_claims,
            trace_id=trace_id,
            grounding="verified" if evidence and not dropped_claims else (
                "partial" if evidence or dropped_claims else "provider_unverified"),
            provider_ttft=result.ttft_seconds,
            provider_seconds=result.request_seconds,
            answer_latency=round(total_seconds, 2))
        if result.ttft_seconds is not None:
            self.state.observe_metric("provider_ttft_seconds", result.ttft_seconds)
        if result.request_seconds is not None:
            self.state.observe_metric("provider_request_seconds", result.request_seconds)
        with self._lock:
            self._last_answer_id = event_id
            self._qa_thread.append({"question": question, "bullets": bullets})
            keep = max(1, self.cfg.max_context_qa)
            del self._qa_thread[:-keep]

    def _refresh_talking_points(self, payload: Dict[str, Any]) -> None:
        started_at = time.time()
        queued_at = float(payload.get("queued_at") or started_at)
        window = payload.get("window") or self._context_text()
        if not window:
            return
        snippets = []
        if self._live_kb is not None or self._kb is not None:
            snippets = self._query_all(window[-1500:], self.cfg.kb_top_k)
        existing = [p["text"] for p in self.state.talking_points()]
        system = self._system_prompt("rolling")
        prompt = self._build_prompt("rolling", None, "", window, snippets, existing=existing)
        result = self._chat("rolling", system, prompt, temperature=0.0)
        self.state.set_meta(
            talking_point_queue_wait=round(max(0.0, started_at - queued_at), 2),
            talking_point_prompt_chars=len(system) + len(prompt),
            talking_point_provider_seconds=(result.request_seconds if result else None))
        self.state.observe_metric("talking_point_queue_wait", max(0.0, started_at - queued_at))
        if result is not None and result.request_seconds is not None:
            self.state.observe_metric("talking_point_provider_seconds", result.request_seconds)
        if result is None:
            self.log("answers: talking-point refresh failed on all providers")
            return
        candidates = parse_point_objects(result.text)
        if not candidates:
            return
        if self.cfg.talking_points_grounded:
            grounded = [
                p for p in candidates
                if grounded_in(p["text"], p["quote"], window,
                               threshold=self.cfg.talking_points_quote_overlap)
            ]
            dropped = len(candidates) - len(grounded)
            if dropped:
                self.log("answers: dropped {} ungrounded talking point(s)".format(dropped))
        else:
            grounded = candidates
        grounded = grounded[: max(1, self.cfg.talking_points_max)]
        if not grounded:
            return
        new_points = self._filter_new_points([p["text"] for p in grounded], existing)
        if not new_points:
            return
        sources = [s.source for s in snippets]
        added = self.state.add_talking_points(
            [text for text, _vec in new_points], sources=sources, model=result.model)
        for text, vec in new_points:
            if vec is not None:
                self._vec_cache[text] = vec
        if added:
            self.log("answers: {} new talking point(s)".format(len(added)))

    def _filter_new_points(self, bullets: List[str],
                           existing: List[str]) -> List[Tuple[str, Optional[List[float]]]]:
        out: List[Tuple[str, Optional[List[float]]]] = []
        known = list(existing)
        for bullet in bullets:
            if is_duplicate_point(bullet, known):
                continue
            vec = self._embed_one(bullet)
            if vec is not None and self._too_similar(vec, known):
                continue
            out.append((bullet, vec))
            known.append(bullet)
        return out

    def _too_similar(self, vec: List[float], known: List[str]) -> bool:
        if self._embedder is None or self.cfg.point_dedupe_score <= 0:
            return False
        for text in known:
            other = self._vec_cache.get(text)
            if other is None:
                other = self._embed_one(text)
                if other is not None:
                    self._vec_cache[text] = other
            if other is not None and _cosine(vec, other) >= self.cfg.point_dedupe_score:
                return True
        return False

    def _embed_one(self, text: str) -> Optional[List[float]]:
        if self._embedder is None:
            return None
        try:
            return self._embedder.encode([text])[0]
        except Exception:  # noqa: BLE001
            return None

    def summarize(self) -> Optional[Dict[str, Any]]:
        """End-of-call summary + action items + follow-up email (best effort)."""
        if (not self.cfg.answers_enabled or not self.cfg.summary_enabled
                or self._chain is None or not self._chain.entries):
            return None
        transcript = self.state.transcript_text().strip()
        if not transcript:
            return None
        points = [p["text"] for p in self.state.talking_points()]
        qa = self._qa_recent()
        system = ("You are wrapping up a live call. Produce a concise JSON object with exactly the "
                  "keys \"summary\" (a short paragraph), \"action_items\" (a list of concrete "
                  "next steps, including owners when known), and \"follow_up_email\" (a short, "
                  "professional plain-text email recapping the call). Respond ONLY with JSON.")
        parts = ["Transcript:\n{}".format(transcript[-12000:])]
        if points:
            parts.append("Talking points raised:\n" + "\n".join("- {}".format(p) for p in points[-30:]))
        if qa:
            qa_text = "\n\n".join(
                "Q: {}\nA: {}".format(q.get("question", ""), " / ".join(q.get("bullets") or []))
                for q in qa[-5:])
            parts.append("Questions asked during the call:\n" + qa_text)
        prompt = "\n\n".join(parts)
        result = self._chat("question", system, prompt,
                            max_tokens=max(self.cfg.answer_max_tokens, 900),
                            temperature=0.3, model_override=self.cfg.summary_model)
        if result is None:
            self.log("summary: all providers failed")
            return None
        summary = parse_summary(result.text)
        self.state.add("summary", summary=summary)
        return summary

    def finish(self) -> Optional[Dict[str, Any]]:
        """Generate the summary before shutdown without ever raising."""
        try:
            # The recorder stops STT first. Drain those final events into the
            # answer buffer before assembling the summary and identity.
            self._drain_events()
            if self._memory_thread is not None:
                self._memory_q.join()
            return self.summarize()
        except Exception as exc:  # noqa: BLE001
            self.log("summary failed: {}".format(exc))
            return None

    def _rewrite_question(self, question: str, context: str) -> str:
        """Resolve a follow-up question into a self-contained one (cheap model)."""
        if self._chain is None or not self._chain.entries:
            return ""
        entry = self._chain.entries[self._chain_index % len(self._chain.entries)]
        system = ("Rewrite the follow-up question as a fully self-contained question using the "
                  "conversation. Keep it short. Reply with only the rewritten question, no quotes "
                  "or preamble.")
        user = "Conversation:\n{}\n\nFollow-up question: {}".format(
            context or "(none)", question)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        est = estimate_tokens(system + user, 80)
        if not self.budget.can_afford(est):
            return ""
        self.budget.reserve(est)
        try:
            result = entry["client"].chat(
                messages, entry["rolling_model"], max_tokens=80, temperature=0.0)
        except LLMError as exc:
            self.budget.record(None, None)
            if exc.status == 429:
                self.budget.pause(exc.retry_after, reason=str(exc))
            self.log("answers: question rewrite failed ({}); using original".format(exc))
            return ""
        except Exception as exc:  # noqa: BLE001
            self.log("answers: question rewrite error ({}); using original".format(exc))
            return ""
        self.budget.record(result.headers, result.usage)
        rewritten = (result.text or "").strip().strip('"').strip()
        return rewritten

    def _system_prompt(self, kind: str) -> str:
        if kind == "question":
            task = ("Answer the most recent question from the conversation. Use the surrounding "
                    "conversation (and the earlier Q&A in this call) to resolve references "
                    "(this, that, it, the other one) and answer the speaker's actual intent, not "
                    "just the literal words. Ground factual claims in the conversation or the "
                    "reference notes and cite the file; do not invent specific numbers, names, or "
                    "integrations. If something is not covered or you are unsure, say so rather "
                    "than guessing.")
            schema = '{"claims": [{"text": "...", "evidence": "exact words", "source": "file.md"}], "sources": ["file.md"], "follow_up": false}'
            tail = ("Each claim must be a single short sentence with an exact evidence span from "
                    "the conversation or reference notes; use an empty source for conversation evidence. "
                    "Set follow_up true only if this "
                    "question is a direct follow-up to the previous question. ")
        else:
            task = ("You are a passive note-taker. Summarize ONLY what the speakers actually said "
                    "in the recent conversation. Use no outside or general knowledge; never invent "
                    "features, integrations, products, plans, or consequences. Every point must be "
                    "directly supported by the transcript and must include a short verbatim quote "
                    "copied from the transcript as evidence. If the conversation is casual, "
                    "fragmentary, or contains nothing substantive, return an empty bullets list -- "
                    "that is the correct answer. At most {} points, and never repeat or paraphrase "
                    "points already shown.".format(max(1, self.cfg.talking_points_max)))
            schema = '{"bullets": [{"text": "...", "quote": "exact words from the transcript"}]}'
            tail = "Each text must be a single short sentence. "
        return (
            "You are a live meeting copilot that fills a side panel during a call. "
            + task +
            " Respond ONLY with JSON of the form " + schema + ". " + tail +
            "No preamble, no markdown."
        )

    def _build_prompt(self, kind: str, question: Optional[str], context: str, window: str,
                      snippets: List[Any], existing: Optional[List[str]] = None,
                      qa: Optional[List[Dict[str, Any]]] = None) -> str:
        parts: List[str] = []
        parts.append("Conversation so far (most recent last):\n{}".format(window or "(none yet)"))
        if qa:
            lines = []
            for item in qa[-3:]:
                answer = " / ".join(item.get("bullets") or [])
                lines.append("Q: {}\nA: {}".format(item.get("question", ""), answer))
            parts.append("Earlier questions this call:\n" + "\n\n".join(lines))
        memory = format_memory(self.state.memory())
        if memory:
            parts.append("Structured meeting memory (exact evidence; do not extend it):\n" + memory)
        if question and context:
            parts.append("Turns immediately before the question:\n{}".format(context))
        if snippets:
            notes = []
            for s in snippets:
                notes.append("[{} — {}] {}".format(s.source, s.heading, s.text[:1200]))
            parts.append("Reference notes:\n" + "\n\n".join(notes))
        else:
            parts.append("Reference notes: (none retrieved)")
        if question:
            parts.append("Question to answer: {}".format(question))
            parts.append("Resolve any references using the conversation above so the answer fits "
                         "what is actually being discussed. Cite reference notes by filename when "
                         "you rely on them.")
        else:
            if existing:
                parts.append("Talking points already shown (do NOT repeat or paraphrase these):\n"
                             + "\n".join("- {}".format(e) for e in existing[-40:]))
            parts.append("Only use the conversation above (and the reference notes if present); "
                         "do not use outside knowledge. Quote the exact transcript words you rely "
                         "on for each point.")
            parts.append("Task: add only NEW talking points that were explicitly said in the "
                         "conversation right now. Return an empty bullets list if there is "
                         "nothing substantive.")
        return "\n\n".join(parts)
