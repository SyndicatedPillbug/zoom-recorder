#!/usr/bin/env python3
"""Create deterministic non-speech WAV fixtures for STT safety tests."""

from __future__ import annotations

import argparse
import random
import struct
import wave
from pathlib import Path
from typing import List, Optional


RATE = 16000


def write_fixture(path: Path, kind: str, duration_seconds: float = 10.0,
                  seed: int = 7) -> Path:
    """Write mono 16-bit PCM silence or seeded low-level noise."""
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if kind not in ("silence", "noise"):
        raise ValueError("kind must be silence or noise")
    count = int(round(RATE * duration_seconds))
    generator = random.Random(seed)
    if kind == "silence":
        pcm = b"\x00\x00" * count
    else:
        pcm = b"".join(struct.pack("<h", generator.randint(-900, 900))
                       for _ in range(count))
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(pcm)
    return target


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Create deterministic STT safety fixtures")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    write_fixture(args.output_dir / "synthetic-silence-10s.wav", "silence",
                  args.duration_seconds)
    write_fixture(args.output_dir / "synthetic-noise-10s.wav", "noise",
                  args.duration_seconds)
    print(str(args.output_dir.expanduser().resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
