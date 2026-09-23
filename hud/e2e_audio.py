#!/usr/bin/env python3
"""Run a real audio-file replay through the production live session path.

Unlike ``hud.lifecycle``, which is deliberately provider-free and deterministic,
this runner uses the configured local whisper.cpp model and the production
``LiveTranscriber`` audio-file source. It is therefore slower and requires
ffmpeg plus a provisioned model, but it verifies the real capture-to-finalize
lane, writeback, and session persistence order.
"""

from __future__ import annotations

import argparse
import json
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import HudConfig
from .identity import count_transcript_words
from .session import LiveSession


def _duration_seconds(audio: Path) -> float:
    with wave.open(str(audio), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError("audio must be mono 16-bit PCM at 16 kHz")
        return source.getnframes() / float(source.getframerate())


def build_config(audio: Path, writeback_dir: Optional[Path] = None) -> HudConfig:
    """Build the safe, local-only config used by a real replay."""
    return HudConfig(
        stt_backend="local",
        audio_file=str(audio),
        answers_enabled=False,
        kb_enabled=False,
        summary_enabled=False,
        diarization_enabled=False,
        open_browser=False,
        persist_seconds=0,
        transcript_writeback_dir=str(writeback_dir) if writeback_dir else None,
    )


def run_audio_e2e(audio: Path, model: Path, outdir: Path,
                  writeback_dir: Optional[Path] = None,
                  drain_seconds: float = 8.0,
                  log=None) -> Dict[str, Any]:
    """Replay ``audio`` through production capture/STT/session finalization."""
    audio = Path(audio).expanduser().resolve()
    model = Path(model).expanduser().resolve()
    outdir = Path(outdir).expanduser().resolve()
    duration = _duration_seconds(audio)
    if not model.is_file():
        raise FileNotFoundError("local STT model not found: {}".format(model))
    # LiveSession normally receives a directory created by the CLI/menu
    # layer.  The replay runner is intentionally standalone, so establish
    # that same invariant before startup can write session identity or HUD
    # artifacts.
    outdir.mkdir(parents=True, exist_ok=True)
    messages: List[str] = []
    logger = log or messages.append
    cfg = build_config(audio, writeback_dir)
    session = LiveSession(cfg, outdir, logger, None, None, model_path=model)
    started = time.perf_counter()
    started_at = session.start()
    if started_at is None:
        raise RuntimeError("live session HTTP server could not start")
    try:
        time.sleep(max(0.0, duration) + max(0.0, float(drain_seconds)))
    finally:
        session.stop()
    snapshot = session.state.snapshot()
    meta = snapshot.get("meta", {})
    return {
        "schema_version": 1,
        "audio": str(audio),
        "model": str(model),
        "audio_duration_seconds": round(duration, 3),
        "wall_seconds": round(time.perf_counter() - started, 3),
        "outdir": str(session.outdir),
        "transcript_words": count_transcript_words(session.state.transcript_text()),
        "transcript_events": len(snapshot.get("transcript", [])),
        "stt_dropped": meta.get("stt_dropped", 0),
        "stt_queued": meta.get("stt_queued", 0),
        "lifecycle_stages": meta.get("lifecycle_stages", {}),
        "writeback_dir": str(writeback_dir) if writeback_dir else None,
        "logs": messages,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a WAV through the production live session")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--writeback-dir", type=Path)
    parser.add_argument("--drain-seconds", type=float, default=8.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run_audio_e2e(args.audio, args.model, args.outdir,
                            args.writeback_dir, args.drain_seconds,
                            log=lambda message: print(message, flush=True))
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
