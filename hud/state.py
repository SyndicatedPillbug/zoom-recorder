#!/usr/bin/env python3
"""In-memory, append-only event log shared by the STT, answer and HTTP threads.

Events are small dicts with a monotonically increasing ``id`` and an epoch
``ts``. The HTTP layer streams them over SSE; nothing here touches disk (the
session flushes a plain-text copy under ``derived/`` on shutdown).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

MAX_EVENTS = 5000


class LiveState:
    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._events: List[Dict[str, Any]] = []
        self._next_id = 1
        self.status: str = "starting"
        self.budget: Dict[str, Any] = {}
        self.meta: Dict[str, Any] = {}

    # -- writes ------------------------------------------------------------
    def add(self, etype: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            event = {"id": self._next_id, "ts": time.time(), "type": etype}
            event.update(fields)
            self._next_id += 1
            self._events.append(event)
            if len(self._events) > MAX_EVENTS:
                del self._events[: len(self._events) - MAX_EVENTS]
            self._lock.notify_all()
            return event

    def set_status(self, status: str, **fields: Any) -> None:
        with self._lock:
            self.status = status
            self.meta.update(fields)
            self._lock.notify_all()

    def set_budget(self, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            self.budget = snapshot
            self._lock.notify_all()

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
                "answers": answers,
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
        lines: List[str] = []
        for e in events:
            stamp = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
            question = e.get("question")
            kind = e.get("kind", "answer")
            heading = question if question else ("Talking points" if kind == "rolling" else "Answer")
            lines.append("## [{}] {}".format(stamp, heading))
            for bullet in e.get("bullets", []):
                lines.append("- {}".format(bullet))
            sources = e.get("sources") or []
            if sources:
                lines.append("")
                lines.append("_Sources: {}_".format(", ".join(sources)))
            lines.append("")
        return "\n".join(lines).strip() + ("\n" if lines else "")

    def timeline_markdown(self, title: str = "") -> str:
        """Interleave transcript lines and answer blocks in chronological order.

        This is the 'transcript + AI answers, relative to the conversation'
        file -- each answer sits directly after the speech that prompted it.
        """
        with self._lock:
            events = [e for e in self._events
                      if e.get("type") in ("transcript", "answer")]
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
