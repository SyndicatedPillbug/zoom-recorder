"""Aggregate redacted answer traces from a completed live session.

The live event log intentionally keeps one event per trace boundary so the HUD
can render progress without waiting for the provider.  This module turns those
events into a stable, provider-free report for long replay comparisons.  It
never includes questions, transcript text, prompts, or retrieved source text.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _percentile(values: List[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 4)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * weight
    return round(value, 4)


def _metric(values: Iterable[Any]) -> Dict[str, Any]:
    numbers = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            numbers.append(number)
    return {
        "count": len(numbers),
        "p50": _percentile(numbers, 0.50),
        "p95": _percentile(numbers, 0.95),
        "max": round(max(numbers), 4) if numbers else None,
    }


def _trace_rows(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for event in events:
        if str(event.get("type") or "") != "answer_trace":
            continue
        trace_id = str(event.get("trace_id") or "").strip()
        if not trace_id:
            continue
        if trace_id not in grouped:
            grouped[trace_id] = {"trace_id": trace_id}
            order.append(trace_id)
        row = grouped[trace_id]
        stage = str(event.get("stage") or "").strip()
        if stage:
            row["stage_{}".format(stage)] = True
        if stage == "queued":
            row["kind"] = str(event.get("kind") or "question")
        if stage == "assembled":
            for key in (
                "queue_wait_seconds", "assembly_seconds", "retrieval_seconds",
                "prompt_chars", "prompt_estimated_tokens", "context_chars",
                "reference_chars", "retrieved_count", "retrieved_chars",
            ):
                if key in event:
                    row[key] = event[key]
        if stage == "provider_complete":
            row["status"] = "complete"
            for key in ("provider_ttft_seconds", "provider_request_seconds", "total_seconds"):
                if key in event:
                    row[key] = event[key]
        if stage == "failed":
            row["status"] = "failed"
            row["failure_reason"] = str(event.get("reason") or "unknown")
            if "total_seconds" in event:
                row["total_seconds"] = event["total_seconds"]
    for trace_id in order:
        row = grouped[trace_id]
        row.setdefault("kind", "question")
        row.setdefault("status", "incomplete")
    return [grouped[trace_id] for trace_id in order]


def build_report(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Return aggregate timing metrics without copying sensitive trace text."""
    rows = _trace_rows(events)
    measured_fields = (
        "queue_wait_seconds", "assembly_seconds", "retrieval_seconds",
        "provider_ttft_seconds", "provider_request_seconds", "total_seconds",
        "prompt_chars", "prompt_estimated_tokens", "context_chars",
        "reference_chars", "retrieved_count", "retrieved_chars",
    )
    metrics = {
        field: _metric(row.get(field) for row in rows if field in row)
        for field in measured_fields
    }
    completed = sum(1 for row in rows if row.get("status") == "complete")
    failed = sum(1 for row in rows if row.get("status") == "failed")
    return {
        "schema_version": 1,
        "trace_count": len(rows),
        "completed_count": completed,
        "failed_count": failed,
        "incomplete_count": len(rows) - completed - failed,
        "metrics": metrics,
        "traces": rows,
    }


def load_events(path: Path) -> List[Dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            events.append(value)
    return events


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate redacted answer-trace timings")
    parser.add_argument("events", type=Path, help="derived/live_events.jsonl")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    rendered = json.dumps(build_report(load_events(args.events)), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
