#!/usr/bin/env python3
"""Paced rolling-window benchmark for the local transcription lane."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .benchmark_manifest import write_result
from .evaluation import word_error_stats
from .stt import LocalWhisperSTT, StablePartialDecoder, looks_hallucinated


def _percentile(values: List[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1,
                max(0, int(math.ceil((percentile / 100.0) * len(ordered))) - 1))
    return round(ordered[index], 6)


def _read_pcm(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError("audio must be mono 16-bit PCM at 16 kHz")
        return source.readframes(source.getnframes()), source.getnframes()


def _load_reference(path: Path) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Load plain reference text or a time-aligned JSON word manifest."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() != ".json":
        return text, None
    payload = json.loads(text)
    words = payload.get("words") if isinstance(payload, dict) else None
    if not isinstance(words, list):
        raise ValueError("timed reference JSON needs a words list")
    normalized = [word for word in words if isinstance(word, dict)
                  and word.get("text") is not None]
    return " ".join(str(word["text"]) for word in normalized), {
        "start_seconds": float(payload.get("start_seconds", 0.0)),
        "words": normalized,
    }


def _window_reference(timed: Dict[str, Any], window_start: float,
                     window_end: float) -> str:
    origin = float(timed["start_seconds"])
    words = []
    for word in timed["words"]:
        try:
            start = float(word["start"])
            end = float(word.get("end", start))
        except (TypeError, ValueError):
            continue
        if end > origin + window_start and start < origin + window_end:
            words.append(str(word["text"]))
    return " ".join(words)


def run_benchmark(audio: Path, model: Path, reference: Optional[Path] = None,
                  window_seconds: float = 4.0,
                  interval_seconds: float = 0.8,
                  pace: bool = True,
                  log: Optional[Callable[[str], None]] = None) -> Dict[str, object]:
    """Run a rolling local benchmark against a paced PCM fixture."""
    if window_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("window and interval must be positive")
    pcm, frame_count = _read_pcm(audio)
    frame_bytes = 2
    duration = frame_count / 16000.0
    window_frames = max(1, int(window_seconds * 16000))
    interval = float(interval_seconds)
    decoder = StablePartialDecoder()
    committed: List[str] = []
    inference_seconds: List[float] = []
    observations = 0
    hallucination_filtered = 0
    latest_hypothesis = ""
    first_stable_word: Optional[str] = None
    first_stable_window_end: Optional[float] = None
    first_stable_publish: Optional[float] = None
    window_wers: List[float] = []
    reference_text = ""
    timed_reference: Optional[Dict[str, Any]] = None
    if reference is not None:
        reference_text, timed_reference = _load_reference(reference)
    started = time.perf_counter()

    def write_log(message: str) -> None:
        if log:
            log(message)

    backend = LocalWhisperSTT(model, write_log)
    try:
        audio_end = window_seconds
        while audio_end <= duration + 1e-6:
            if pace:
                target = started + audio_end
                remaining = target - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)
            end_frame = min(frame_count, int(round(audio_end * 16000)))
            start_frame = max(0, end_frame - window_frames)
            chunk = pcm[start_frame * frame_bytes:end_frame * frame_bytes]
            request_started = time.perf_counter()
            result = backend.transcribe(chunk)
            published = time.perf_counter()
            inference = published - request_started
            inference_seconds.append(inference)
            text = str(getattr(result, "text", "") or "").strip()
            if text and looks_hallucinated(text, marginal=True):
                hallucination_filtered += 1
                text = ""
            if text:
                observations += 1
                stable = decoder.accept(text)
                latest_hypothesis = " ".join(decoder.current).strip()
                if timed_reference is not None:
                    window_text = _window_reference(
                        timed_reference, max(0.0, audio_end - window_seconds), audio_end)
                    if window_text:
                        window_wers.append(word_error_stats(window_text, text).wer or 0.0)
                if stable:
                    committed.append(stable)
                    if first_stable_word is None:
                        first_stable_word = stable.split()[0]
                        first_stable_window_end = audio_end
                        first_stable_publish = published - started
            audio_end += interval
    finally:
        backend.close()

    committed_text = " ".join(committed).strip()
    output: Dict[str, object] = {
        "benchmark_schema_version": 1,
        "audio": str(audio),
        "model": str(model),
        "duration_seconds": round(duration, 6),
        "window_seconds": window_seconds,
        "interval_seconds": interval_seconds,
        "paced": pace,
        "observations": observations,
        "hallucination_filtered": hallucination_filtered,
        "committed_words": len(committed_text.split()),
        "first_stable_word": first_stable_word,
        "first_stable_window_end_seconds": first_stable_window_end,
        "first_stable_publish_seconds": first_stable_publish,
        "first_stable_window_to_publish_seconds": (
            round(first_stable_publish - first_stable_window_end, 6)
            if (pace and first_stable_publish is not None
                and first_stable_window_end is not None)
            else None),
        "inference_p50_seconds": _percentile(inference_seconds, 50),
        "inference_p95_seconds": _percentile(inference_seconds, 95),
        "inference_mean_seconds": round(statistics.mean(inference_seconds), 6)
        if inference_seconds else None,
        "committed_text": committed_text,
        "latest_hypothesis": latest_hypothesis,
    }
    if reference is not None:
        output["stable_only_wer"] = word_error_stats(
            reference_text, committed_text).as_dict()
        output["window_wer_samples"] = len(window_wers)
        output["window_wer_p50"] = _percentile(window_wers, 50)
        output["window_wer_p95"] = _percentile(window_wers, 95)
    return output


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark paced local rolling transcription")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--window-seconds", type=float, default=4.0)
    parser.add_argument("--interval-seconds", type=float, default=0.8)
    parser.add_argument("--output", type=Path,
                        help="atomically write the complete JSON result to this path")
    parser.add_argument("--no-pace", action="store_true",
                        help="run as fast as possible instead of simulating live audio")
    args = parser.parse_args(argv)
    result = run_benchmark(
        args.audio, args.model, args.reference,
        window_seconds=args.window_seconds,
        interval_seconds=args.interval_seconds,
        pace=not args.no_pace,
        log=lambda message: print(message, flush=True),
    )
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        write_result(args.output, result)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
