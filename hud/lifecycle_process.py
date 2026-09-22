#!/usr/bin/env python3
"""Process-level forced-stop/restart fixture for lifecycle verification."""

from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from pathlib import Path
from typing import List, Optional

from .config import HudConfig
from .lifecycle import _FixtureAnswers, _FixtureSTT, _apply_event
from .replay import load_jsonl
from .session import LiveSession
from .transcript_writeback import TranscriptWriteback


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a signal-aware lifecycle fixture process")
    parser.add_argument("events", type=Path)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--writeback-dir", type=Path)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.05)
    args = parser.parse_args(argv)

    stopping = threading.Event()

    def _handle_stop(_signum, _frame) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    messages: List[str] = []
    cfg = HudConfig(answers_enabled=False, kb_enabled=False,
                    summary_enabled=False, open_browser=False,
                    diarization_enabled=False,
                    transcript_writeback_dir=(str(args.writeback_dir)
                                              if args.writeback_dir else None))
    outdir = args.outdir.expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    session = LiveSession(cfg, outdir, messages.append, None, None)
    session._started = True
    session.stt = _FixtureSTT()
    session.answers = _FixtureAnswers()
    if args.writeback_dir is not None:
        session._writeback = TranscriptWriteback(
            session.state, str(args.writeback_dir), outdir.name, messages.append)
        if not session._writeback.start():
            session._writeback = None

    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    args.ready_file.write_text(json.dumps({"pid": __import__("os").getpid()}) + "\n",
                                encoding="utf-8")
    try:
        for event in load_jsonl(args.events):
            if stopping.wait(max(0.0, args.interval)):
                break
            _apply_event(session, event)
    finally:
        session.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
