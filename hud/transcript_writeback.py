#!/usr/bin/env python3
"""Asynchronous live-transcript mirror.

The normal recording/HUD persistence path remains authoritative.  This module
is an optional second sink that consumes the shared event log on its own daemon
thread, so filesystem latency or a TCC denial can never block audio capture,
STT, or answer generation.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from .state import LiveState


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class TranscriptWriteback:
    """Mirror live transcript events to one Markdown document."""

    def __init__(self, state: LiveState, directory: str, session_name: str,
                 log: Callable[[str], None]) -> None:
        self.state = state
        self.directory = Path(directory).expanduser()
        safe_name = _SAFE_NAME_RE.sub("-", session_name).strip(".-") or "session"
        self.path = self.directory / (safe_name + "-live-transcript.md")
        self.log = log
        self._cursor = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fh: Optional[Any] = None
        self._disabled = False

    def start(self) -> bool:
        """Start the mirror thread without doing filesystem work on the caller."""
        self._thread = threading.Thread(target=self._run, name="transcript-writeback",
                                         daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Drain events produced before shutdown, then close the file."""
        if self._thread is None:
            self._close()
            return
        self._stop.set()
        self._thread.join(timeout=3.0)
        if self._thread.is_alive():
            self.log("live transcript writeback: shutdown drain timed out")
        self._thread = None
        self._close()

    def finalize_name(self, slug: str) -> None:
        """Rename the completed mirror after the session topic is known."""
        if not slug or self._fh is not None:
            return
        target = self.path.with_name("{}-live-transcript.md".format(slug))
        if target == self.path:
            return
        try:
            suffix = 1
            candidate = target
            while candidate.exists():
                suffix += 1
                candidate = target.with_name(
                    "{}-{}{}".format(target.stem, suffix, target.suffix))
            self.path.rename(candidate)
            self.path = candidate
        except OSError as exc:
            self.log("live transcript writeback kept original name ({}): {}".format(
                self.path, exc))

    def _run(self) -> None:
        if not self._open():
            return
        self.log("live transcript writeback: {}".format(self.path))
        while True:
            events = self.state.wait_for(self._cursor, timeout=0.5)
            self._consume(events)
            if self._stop.is_set():
                # LiveSession stops STT before stopping this sink, so this
                # final read captures the last recognized event deterministically.
                self._consume(self.state.since(self._cursor))
                self._close()
                return

    def _open(self) -> bool:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            base = self.path
            suffix = 1
            while True:
                candidate = base if suffix == 1 else base.with_name(
                    "{}-{}{}".format(base.stem, suffix, base.suffix))
                try:
                    # Exclusive creation prevents two recorder sessions with
                    # the same display name from ever sharing a document.
                    self._fh = candidate.open("x", encoding="utf-8", buffering=1)
                    self.path = candidate
                    self._fh.write("# Live transcript\n\n")
                    self._fh.flush()
                    break
                except FileExistsError:
                    suffix += 1
            return True
        except OSError as exc:
            self._disabled = True
            self.log("live transcript writeback disabled ({}): {}".format(
                self.path, exc))
            self._close()
            return False

    def _consume(self, events: List[dict]) -> None:
        if self._disabled:
            return
        for event in events:
            self._cursor = max(self._cursor, int(event.get("id", 0)))
            if event.get("type") != "transcript":
                continue
            text = str(event.get("text") or "").strip()
            if not text or self._fh is None:
                continue
            stamp = time.strftime("%H:%M:%S", time.localtime(event.get("ts", time.time())))
            speaker = str(event.get("speaker") or "").strip()
            prefix = "**{}:** ".format(speaker) if speaker else ""
            try:
                self._fh.write("[{}] {}{}\n".format(stamp, prefix, text))
                self._fh.flush()
            except OSError as exc:
                self._disabled = True
                self.log("live transcript writeback stopped ({}): {}".format(
                    self.path, exc))
                self._close()
                return

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
