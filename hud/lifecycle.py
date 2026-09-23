#!/usr/bin/env python3
"""Provider-free end-to-end session lifecycle harness.

This harness exercises the production ``LiveSession.stop`` persistence order
with deterministic transcript events. It intentionally replaces capture, STT,
and the answer provider with inert doubles; the purpose is lifecycle and
artifact verification, not model-quality benchmarking.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .config import HudConfig
from .identity import count_transcript_words
from .replay import load_jsonl
from .session import LiveSession
from .transcript_writeback import TranscriptWriteback


class _FixtureSTT:
    def __init__(self, failure: Optional[str] = None) -> None:
        self.failure = failure

    def stop(self) -> None:
        if self.failure == "stt":
            raise RuntimeError("injected local STT shutdown failure")
        return None


class _FixtureAnswers:
    def __init__(self, failure: Optional[str] = None) -> None:
        self.failure = failure

    def finish(self) -> None:
        if self.failure == "provider":
            raise RuntimeError("injected provider timeout")
        return None

    def stop(self) -> None:
        return None


def _apply_event(session: LiveSession, raw: Dict[str, Any]) -> None:
    event = dict(raw)
    event_type = str(event.pop("type", "event"))
    event.pop("id", None)
    if event_type == "transcript_partial":
        source_key = str(event.pop("source_key", "mixed"))
        session.state.set_transcript_partial(source_key, **event)
        return
    if event_type == "speaker_mapping":
        session.state.set_speaker_label(
            str(event.get("speaker_id") or ""), str(event.get("label") or ""),
            source=str(event.get("source") or "replay"))
        return
    if event_type == "talking_point":
        session.state.add_talking_points(
            [str(event.get("text") or "")],
            sources=event.get("sources") or [],
            model=str(event.get("model") or "replay"))
        return
    if event_type == "memory":
        session.state.add_memory_item(event)
        return
    if event_type == "status":
        session.state.set_status(str(event.get("status") or "replay"),
                                 **(event.get("meta") or {}))
        return
    session.state.add(event_type, **event)


def run_fixture(events: Iterable[Dict[str, Any]], outdir: Path,
                writeback_dir: Optional[Path] = None,
                inject_failure: Optional[str] = None,
                log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Run deterministic events through production persistence/shutdown code."""
    messages: List[str] = []
    logger = log or messages.append
    original = Path(outdir).expanduser().resolve()
    original.mkdir(parents=True, exist_ok=True)
    cfg = HudConfig(
        answers_enabled=False,
        kb_enabled=False,
        summary_enabled=False,
        open_browser=False,
        diarization_enabled=False,
        transcript_writeback_dir=str(writeback_dir) if writeback_dir else None,
    )
    session = LiveSession(cfg, original, logger, None, None,
                          started_at=None)
    session._started = True
    session.stt = _FixtureSTT(inject_failure)
    session.answers = _FixtureAnswers(inject_failure)
    if writeback_dir is not None:
        sink_dir = Path(writeback_dir)
        if inject_failure in ("writeback", "permission"):
            sink_dir.parent.mkdir(parents=True, exist_ok=True)
            sink_dir.write_text("blocked", encoding="utf-8")
        session._writeback = TranscriptWriteback(
            session.state, str(sink_dir), original.name, logger)
        if not session._writeback.start():
            session._writeback = None
    for raw in events:
        _apply_event(session, dict(raw))
    session.stop()
    return {
        "original_outdir": str(original),
        "outdir": str(session.outdir),
        "transcript_words": count_transcript_words(session.state.transcript_text()),
        "latest_event_id": session.state.latest_id(),
        "writeback": str(session._writeback.path) if session._writeback else None,
        "status": session.state.status,
        "logs": messages,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a provider-free lifecycle fixture")
    parser.add_argument("events", type=Path, help="JSONL transcript/event fixture")
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--writeback-dir", type=Path)
    parser.add_argument("--inject-failure", choices=("provider", "stt", "writeback", "permission"))
    args = parser.parse_args(argv)
    result = run_fixture(load_jsonl(args.events), args.outdir, args.writeback_dir,
                         inject_failure=args.inject_failure,
                         log=lambda message: print(message, flush=True))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
