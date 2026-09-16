#!/usr/bin/env python3
"""Generative answer engine for the right-hand HUD column.

Two triggers:
  * a question detected in the rolling transcript -> answered immediately with
    the higher-quality model;
  * a periodic (default 35 s) refresh -> short talking points from the cheaper,
    high-quota model.

Both are grounded in the recent conversation plus retrieved ``.md`` snippets.
All provider calls are governed by :class:`hud.budget.BudgetGovernor` so an
aggressive cadence degrades gracefully instead of hammering a free tier.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .budget import BudgetGovernor
from .config import HudConfig, get_provider
from .kb import KBIndex
from .llm import LLMClient, LLMError
from .state import LiveState

QUESTION_WORDS = re.compile(
    r"^\s*(what|how|why|when|who|where|which|can|could|would|should|do|does|did|"
    r"is|are|was|were|will|may|might|tell me|explain|walk me|give me)\b",
    re.I,
)
QUESTION_PHRASES = re.compile(
    r"\b(tell me about|explain|what's|whats|how do|how does|how would|"
    r"can you|could you|would you|do you|did you|are you|is there|"
    r"any thoughts|your take|walk me through)\b",
    re.I,
)
BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")


def detect_question(text: str) -> Optional[str]:
    """Return the most recent question-like sentence, or None."""
    if not text:
        return None
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    candidate: Optional[str] = None
    for sentence in sentences:
        s = sentence.strip()
        if not s:
            continue
        if s.endswith("?") or QUESTION_WORDS.match(s) or QUESTION_PHRASES.search(s):
            candidate = s
    return candidate


def parse_bullets(text: str) -> List[str]:
    """Parse bullets from a JSON object (preferred) or plain markdown."""
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            bullets = obj.get("bullets") or obj.get("points") or []
            if isinstance(bullets, list):
                return [str(b).strip() for b in bullets if str(b).strip()]
        except ValueError:
            pass
    bullets: List[str] = []
    for line in text.splitlines():
        m = BULLET_RE.match(line)
        if m:
            bullets.append(m.group(1).strip())
    if not bullets:
        # Fall back to sentences so we never show an empty card.
        bullets = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    return bullets[:6]


def estimate_tokens(text: str, max_tokens: int) -> int:
    return max(1, len(text) // 4) + max_tokens


class _Chain:
    """Ordered provider chain with model selection per role."""

    def __init__(self, entries: List[Dict[str, Any]]) -> None:
        self.entries = entries

    def __bool__(self) -> bool:
        return bool(self.entries)


class AnswerEngine:
    def __init__(self, state: LiveState, log: Callable[[str], None],
                 cfg: HudConfig, budget: Optional[BudgetGovernor] = None) -> None:
        self.state = state
        self.log = log
        self.cfg = cfg
        self.budget = budget or BudgetGovernor()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._id_cursor = 0
        self._buffer: List[Tuple[float, str]] = []
        self._last_rolling_ts = 0.0
        self._last_rolling_hash = ""
        self._last_question_ts = 0.0
        self._last_question_hash = ""
        self._kb: Optional[KBIndex] = None
        self._chain: Optional[_Chain] = None
        self._chain_index = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hud-answers", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)

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

    def _setup_kb(self) -> None:
        if not self.cfg.kb_enabled or not self.cfg.kb_dirs:
            return
        self._kb = KBIndex(self.cfg.kb_dirs, self.cfg.kb_model,
                           self.cfg.kb_cache_dir, self.log)
        if not self._kb.available():
            self._kb = None
            return
        self._kb.build(force=self.cfg.kb_reindex)

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

        self._setup_kb()
        self._id_cursor = self.state.latest_id()
        self.state.set_budget(self.budget.snapshot())
        self.log("answers enabled via {} (chat={}, rolling={}, interval={:.0f}s)".format(
            self.cfg.answers_backend, self._chain.entries[0]["chat_model"],
            self._chain.entries[0]["rolling_model"], self.cfg.answer_interval))

        while not self._stop.wait(1.0):
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self.log("answers loop error: {}".format(exc))
        self._flush_buffer()

    def _tick(self) -> None:
        self._drain_events()
        self._trim_buffer()
        self.state.set_budget(self.budget.snapshot())

        if self.budget.blocked_seconds() > 0:
            return
        now = time.time()
        # Questions take priority and use the better model.
        latest = self._buffer[-1][1] if self._buffer else ""
        question = detect_question(latest)
        if (question and now - self._last_question_ts >= self.cfg.question_cooldown
                and self._question_hash(question) != self._last_question_hash):
            if self._answer(kind="question", question=question):
                self._last_question_ts = now
                self._last_question_hash = self._question_hash(question)
            return

        if not self.cfg.rolling_enabled:
            return
        if now - self._last_rolling_ts < self.cfg.answer_interval:
            return
        window = self._context_text()
        if not window:
            self._last_rolling_ts = now
            return
        digest = str(hash(window))
        if digest == self._last_rolling_hash:
            self._last_rolling_ts = now
            return
        if self.budget.daily_budget_low():
            # Degrade rolling refreshes first; keep question answers working.
            self._last_rolling_ts = now
            return
        if self._answer(kind="rolling", question=None):
            self._last_rolling_ts = now
            self._last_rolling_hash = digest

    # -- transcript buffer -------------------------------------------------
    def _drain_events(self) -> None:
        events = self.state.since(self._id_cursor)
        for event in events:
            self._id_cursor = max(self._id_cursor, int(event.get("id", 0)))
            if event.get("type") == "transcript" and event.get("source") == "live":
                self._buffer.append((float(event.get("ts", time.time())),
                                     str(event.get("text", ""))))

    def _trim_buffer(self) -> None:
        cutoff = time.time() - self.cfg.context_minutes * 60.0
        self._buffer = [(ts, t) for ts, t in self._buffer if ts >= cutoff][-400:]

    def _context_text(self) -> str:
        return " ".join(t for _ts, t in self._buffer).strip()

    def _question_hash(self, question: str) -> str:
        return str(hash(question.lower()))

    def _flush_buffer(self) -> None:
        self._buffer = []

    # -- generation --------------------------------------------------------
    def _answer(self, kind: str, question: Optional[str]) -> bool:
        window = self._context_text()
        if not window and not question:
            return False
        snippets = []
        if self._kb is not None:
            query_text = question or window[-1500:]
            snippets = self._kb.query(query_text, self.cfg.kb_top_k)
        prompt = self._build_prompt(kind, question, window, snippets)
        est = estimate_tokens(prompt, self.cfg.answer_max_tokens)

        for offset in range(len(self._chain.entries)):  # type: ignore[union-attr]
            entry = self._chain.entries[(self._chain_index + offset) % len(self._chain.entries)]  # type: ignore[union-attr]
            model = entry["rolling_model"] if kind == "rolling" else entry["chat_model"]
            if not self.budget.can_afford(est):
                return False
            self.budget.reserve(est)
            messages = [
                {"role": "system", "content": self._system_prompt(kind)},
                {"role": "user", "content": prompt},
            ]
            response_format = {"type": "json_object"} if entry.get("structured") else None
            try:
                try:
                    result = entry["client"].chat(
                        messages, model, max_tokens=self.cfg.answer_max_tokens,
                        temperature=0.2, response_format=response_format)
                except LLMError as exc:
                    if exc.status == 400 and response_format is not None:
                        # Reasoning models (e.g. Groq's gpt-oss-120b) can emit
                        # truncated/invalid JSON under strict json mode, which
                        # Groq rejects with a 400. Retry as plain text; the
                        # bullet parser handles JSON-in-prose or plain bullets.
                        self.log("answers: {} JSON mode rejected; retrying plain text".format(
                            entry["name"]))
                        result = entry["client"].chat(
                            messages, model, max_tokens=self.cfg.answer_max_tokens,
                            temperature=0.2, response_format=None)
                    else:
                        raise
            except LLMError as exc:
                self.budget.record(None, None)
                if exc.status == 429:
                    self.budget.pause(exc.retry_after, reason=str(exc))
                    self.log("answers: rate limited by {}; backing off".format(entry["name"]))
                    return False
                self.log("answers: {} error: {}".format(entry["name"], exc))
                continue
            except Exception as exc:  # noqa: BLE001
                self.log("answers: {} unexpected error: {}".format(entry["name"], exc))
                continue
            self.budget.record(result.headers, result.usage)
            self._chain_index = (self._chain_index + offset) % len(self._chain.entries)  # type: ignore[union-attr]
            bullets = parse_bullets(result.text)
            if not bullets:
                continue
            self.state.add(
                "answer", kind=kind, question=question, bullets=bullets,
                sources=[s.source for s in snippets], model=result.model,
                usage=result.usage)
            self.state.set_budget(self.budget.snapshot())
            return True

        self.state.add("answer", kind=kind, question=question, bullets=[],
                       sources=[], model="", error="all providers failed")
        return False

    def _system_prompt(self, kind: str) -> str:
        if kind == "question":
            task = ("Answer the most recent question from the conversation. Be accurate and "
                    "concrete; if the reference notes are relevant, use and cite them.")
        else:
            task = ("Surface 3-5 concise, useful talking points or key facts relevant to what "
                    "is being discussed right now. Prioritize anything that helps the listener "
                    "contribute. Use the reference notes when relevant and cite them.")
        return (
            "You are a live meeting copilot that fills a side panel during a call. "
            + task +
            " Respond ONLY with JSON of the form "
            '{"bullets": ["...", "..."], "sources": ["file.md"]}. '
            "Each bullet must be a single short sentence. No preamble, no markdown."
        )

    def _build_prompt(self, kind: str, question: Optional[str], window: str,
                      snippets: List[Any]) -> str:
        parts: List[str] = []
        parts.append("Recent conversation:\n{}".format(window[-4000:] or "(none yet)"))
        if snippets:
            notes = []
            for s in snippets:
                notes.append("[{} — {}] {}".format(s.source, s.heading, s.text[:1200]))
            parts.append("Reference notes:\n" + "\n\n".join(notes))
        if question:
            parts.append("Question to answer: {}".format(question))
        else:
            parts.append("Task: provide the most useful current talking points.")
        return "\n\n".join(parts)
