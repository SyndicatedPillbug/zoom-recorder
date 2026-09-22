#!/usr/bin/env python3
"""In-memory, append-only event log shared by the STT, answer and HTTP threads.

Events are small dicts with a monotonically increasing ``id`` and an epoch
``ts``. The HTTP layer streams them over SSE; nothing here touches disk (the
session flushes a plain-text copy under ``derived/`` on shutdown).
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

MAX_EVENTS = 5000

# Talking points live in their own canonical list so they are never evicted from
# the ring buffer and survive a page reload. Near-duplicates are suppressed both
# by exact normalized match and by token overlap.
_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
POINT_SIMILARITY = 0.8


def normalize_point(text: str) -> str:
    return _PUNCT_RE.sub("", (text or "").lower()).strip()


def _point_tokens(text: str) -> set:
    return set(normalize_point(text).split())


def is_duplicate_point(text: str, existing: Sequence[str],
                       threshold: float = POINT_SIMILARITY) -> bool:
    norm = normalize_point(text)
    if not norm:
        return True
    tokens = _point_tokens(text)
    for other in existing:
        if normalize_point(other) == norm:
            return True
        other_tokens = _point_tokens(other)
        if tokens and other_tokens:
            overlap = len(tokens & other_tokens)
            union = len(tokens | other_tokens)
            if union and overlap / union >= threshold:
                return True
    return False


class LiveState:
    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._events: List[Dict[str, Any]] = []
        self._talking_points: List[Dict[str, Any]] = []
        self._partials: Dict[str, Dict[str, Any]] = {}
        self._next_id = 1
        self.status: str = "starting"
        self.budget: Dict[str, Any] = {}
        self.meta: Dict[str, Any] = {}

    # -- writes ------------------------------------------------------------
    def _append_locked(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event = dict(event)
        event["id"] = self._next_id
        event.setdefault("ts", time.time())
        self._next_id += 1
        self._events.append(event)
        if len(self._events) > MAX_EVENTS:
            del self._events[: len(self._events) - MAX_EVENTS]
        self._lock.notify_all()
        return event

    def add(self, etype: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            event = {"type": etype}
            event.update(fields)
            return self._append_locked(event)

    def set_status(self, status: str, **fields: Any) -> None:
        with self._lock:
            self.status = status
            self.meta.update(fields)
            self._append_locked({"type": "status", "status": status, "meta": dict(self.meta)})

    def set_meta(self, **fields: Any) -> None:
        # Emitted as an event so lag/pause updates reach SSE clients live, not
        # just on the initial snapshot.
        with self._lock:
            self.meta.update(fields)
            self._append_locked({"type": "meta", "meta": dict(fields)})

    def add_talking_points(self, points: Sequence[str], sources: Optional[Sequence[str]] = None,
                           model: str = "") -> List[Dict[str, Any]]:
        """Append genuinely new talking points; suppress near-duplicates.

        Returns the events that were actually added. Points are append-only and
        kept in a canonical list outside the evictable event ring.
        """
        src = list(sources or [])
        added: List[Dict[str, Any]] = []
        with self._lock:
            existing = [p["text"] for p in self._talking_points]
            for raw in points:
                text = str(raw).strip()
                if not text or is_duplicate_point(text, existing):
                    continue
                event = {
                    "id": self._next_id, "ts": time.time(), "type": "talking_point",
                    "text": text, "sources": src, "model": model,
                }
                self._next_id += 1
                self._events.append(event)
                if len(self._events) > MAX_EVENTS:
                    del self._events[: len(self._events) - MAX_EVENTS]
                self._talking_points.append(
                    {"text": text, "ts": event["ts"], "sources": src})
                existing.append(text)
                added.append(event)
            if added:
                self._lock.notify_all()
        return added

    def set_budget(self, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            self.budget = snapshot
            self._lock.notify_all()

    def update_answer(self, event_id: int, **fields: Any) -> None:
        """Update an existing answer event in place (for streaming).

        Replaces the named fields on the event and emits an 'answer_update'
        event so SSE clients can patch the live card without waiting for a
        full new event.
        """
        with self._lock:
            for e in self._events:
                if e.get("id") == event_id:
                    e.update(fields)
                    break
            self._append_locked({
                "type": "answer_update", "ref": event_id,
                "fields": dict(fields),
            })

    def set_transcript_partial(self, source_key: str, text: str,
                               speaker: Optional[str] = None,
                               revision: int = 0, **fields: Any) -> Dict[str, Any]:
        """Publish a replaceable, provisional transcript draft.

        Partial speech is intentionally kept out of the authoritative
        transcript helpers. Consumers may render it, but writeback, retrieval,
        summaries, and answer evidence only consume ``transcript`` events.
        """
        key = str(source_key or "mixed")
        with self._lock:
            event = {"type": "transcript_partial", "source_key": key,
                     "text": str(text or ""), "speaker": speaker,
                     "revision": int(revision), "provisional": True}
            event.update(fields)
            self._partials[key] = dict(event)
            return self._append_locked(event)

    # -- reads -------------------------------------------------------------
    def latest_id(self) -> int:
        with self._lock:
            return self._next_id - 1

    def since(self, event_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["id"] > event_id]

    def wait_for(self, event_id: int, timeout: float = 15.0) -> List[Dict[str, Any]]:
        """Block until a new event arrives after ``event_id`` or timeout."""
        with self._lock:
            if not any(e["id"] > event_id for e in self._events):
                self._lock.wait(timeout)
            return [e for e in self._events if e["id"] > event_id]

    def talking_points(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(p) for p in self._talking_points]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            transcript = [e for e in self._events if e.get("type") == "transcript"]
            answers = [e for e in self._events if e.get("type") == "answer"]
            return {
                "status": self.status,
                "budget": self.budget,
                "meta": self.meta,
                "latest_id": self._next_id - 1,
                "transcript": transcript,
                "partials": [dict(p) for p in self._partials.values()],
                "answers": answers,
                "talking_points": [dict(p) for p in self._talking_points],
            }

    def transcript_text(self, since_ts: Optional[float] = None) -> str:
        with self._lock:
            events = [e for e in self._events if e.get("type") == "transcript"]
        if since_ts is not None:
            events = [e for e in events if e.get("ts", 0) >= since_ts]
        lines: List[str] = []
        for e in events:
            text = str(e.get("text", "")).strip()
            if not text:
                continue
            stamp = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
            speaker = e.get("speaker")
            prefix = "{}: ".format(speaker) if speaker else ""
            lines.append("[{}] {}{}".format(stamp, prefix, text))
        return "\n".join(lines)

    def answers_markdown(self) -> str:
        with self._lock:
            events = [e for e in self._events if e.get("type") == "answer"]
            points = [dict(p) for p in self._talking_points]
        lines: List[str] = []
        for e in events:
            stamp = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
            question = e.get("question")
            heading = question if question else "Answer"
            lines.append("## [{}] {}".format(stamp, heading))
            for bullet in e.get("bullets", []):
                lines.append("- {}".format(bullet))
            sources = e.get("sources") or []
            if sources:
                lines.append("")
                lines.append("_Sources: {}_".format(", ".join(sources)))
            lines.append("")
        if points:
            lines.append("## Talking points")
            for point in points:
                stamp = time.strftime("%H:%M:%S", time.localtime(point.get("ts", 0)))
                suffix = ""
                src = point.get("sources") or []
                if src:
                    suffix = " _({})_".format(", ".join(src))
                lines.append("- [{}] {}{}".format(stamp, point["text"], suffix))
            lines.append("")
        return "\n".join(lines).strip() + ("\n" if lines else "")

    def timeline_markdown(self, title: str = "") -> str:
        """Interleave transcript lines and answer blocks in chronological order.

        This is the 'transcript + AI answers, relative to the conversation'
        file -- each answer sits directly after the speech that prompted it.
        """
        with self._lock:
            events = [e for e in self._events
                      if e.get("type") in ("transcript", "answer", "talking_point")]
        if not events:
            return ""
        lines: List[str] = []
        if title:
            lines.append("# {}".format(title))
            lines.append("")
        for e in events:
            stamp = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
            if e["type"] == "transcript":
                text = str(e.get("text", "")).strip()
                if text:
                    speaker = e.get("speaker")
                    prefix = "{}: ".format(speaker) if speaker else ""
                    lines.append("[{}] {}{}".format(stamp, prefix, text))
                continue
            if e["type"] == "talking_point":
                text = str(e.get("text", "")).strip()
                if text:
                    lines.append("[{}] + {}".format(stamp, text))
                continue
            question = e.get("question")
            kind = e.get("kind", "answer")
            heading = question if question else ("Talking points" if kind == "rolling" else "Answer")
            lines.append("")
            lines.append("[{}] **{}**".format(stamp, heading))
            for bullet in e.get("bullets", []):
                lines.append("- {}".format(bullet))
            sources = e.get("sources") or []
            if sources:
                lines.append("")
                lines.append("_Sources: {}_".format(", ".join(sources)))
            lines.append("")
        return "\n".join(lines).strip() + "\n"
