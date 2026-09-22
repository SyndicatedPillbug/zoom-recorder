#!/usr/bin/env python3
"""Deterministic transcript-event replay and latency measurements.

Live sessions can save their event stream as JSONL. This module replays that
stream without audio devices or a provider, making question finalization,
memory extraction, and event-shape regressions repeatable on another Mac.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .state import LiveState
from .memory import extract_memory


def write_jsonl(path: Path, events: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
    return events


def replay(events: Iterable[Dict[str, Any]]) -> LiveState:
    state = LiveState()
    for event in events:
        item = dict(event)
        etype = str(item.pop("type", "event"))
        item.pop("id", None)
        item.pop("ts", None)
        added = state.add(etype, **item)
        if etype == "transcript" and item.get("finalized", True):
            for memory_item in extract_memory(
                    str(item.get("text") or ""), str(item.get("speaker") or ""),
                    float(added.get("ts") or time.time()), int(added.get("id") or 0)):
                state.add_memory_item(memory_item)
    return state


def benchmark(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    started = time.perf_counter()
    state = replay(events)
    elapsed = time.perf_counter() - started
    transcript = state.snapshot()["transcript"]
    return {
        "events": len(events),
        "transcript_events": len(transcript),
        "transcript_words": sum(len(str(e.get("text") or "").split()) for e in transcript),
        "memory_items": len(state.memory()),
        "replay_seconds": round(elapsed, 6),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a zoom-recorder JSONL event fixture")
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(benchmark(load_jsonl(args.fixture)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
