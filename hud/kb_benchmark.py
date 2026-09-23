#!/usr/bin/env python3
"""Measure bounded knowledge retrieval as a vault grows.

This benchmark uses the production :class:`KBIndex` with deterministic hashing
embeddings and synthetic Obsidian-like metadata. It intentionally measures
query time after indexing, not embedding or first-build time, so it answers the
operational question that matters during a call: can retrieval stay out of the
answer latency budget for a large vault?
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .kb import HashingEmbedder, KBIndex


def _percentile(values: List[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * weight)


def run_benchmark(chunk_count: int = 6000, query_count: int = 40,
                  top_k: int = 5, scope_tag: Optional[str] = None) -> Dict[str, Any]:
    """Return a reproducible retrieval-latency result without network access."""
    started = time.perf_counter()
    chunks: List[Dict[str, Any]] = []
    for number in range(max(1, int(chunk_count))):
        tag = "enterprise" if number % 4 == 0 else "general"
        chunks.append({
            "source": "note-{}.md".format(number // 4),
            "heading": "Planning topic {}".format(number),
            "text": "Planning discussion {} covers pricing rollout and timeline.".format(number),
            "metadata": {"tags": tag},
            "links": ["Pricing plan" if number % 3 == 0 else "Roadmap"],
        })

    with tempfile.TemporaryDirectory(prefix="zoom-recorder-kb-benchmark-") as tmp:
        index = KBIndex([], HashingEmbedder(dim=256), cache_dir=str(Path(tmp) / "cache"),
                        log=lambda _message: None, min_score=0.0,
                        scope_tags=[scope_tag] if scope_tag else None)
        index.init_empty()
        indexed = index.add_chunks(chunks)
        # Remove the first query's one-time SQLite/page-cache cost from the
        # reported samples without hiding it from the setup duration.
        index.query("pricing rollout timeline", top_k=top_k)
        samples: List[float] = []
        hits = 0
        for number in range(max(1, int(query_count))):
            query = "pricing rollout timeline topic {}".format(number % 100)
            query_started = time.perf_counter()
            hits += len(index.query(query, top_k=top_k))
            samples.append(time.perf_counter() - query_started)

    return {
        "schema_version": 1,
        "benchmark": "kb-retrieval",
        "chunk_count": indexed,
        "query_count": len(samples),
        "top_k": int(top_k),
        "scope_tag": scope_tag,
        "backend": "hashing-256 + sqlite-fts5-candidate-filter",
        "hardware": "unknown",
        "setup_seconds": round(time.perf_counter() - started, 4),
        "query_seconds": {
            "p50": round(statistics.median(samples), 6) if samples else None,
            "p95": round(_percentile(samples, 0.95), 6) if samples else None,
            "max": round(max(samples), 6) if samples else None,
        },
        "hits": hits,
        "memory_mb": None,
        "notes": "Synthetic retrieval benchmark; compare on target hardware before changing defaults.",
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", type=int, default=6000)
    parser.add_argument("--queries", type=int, default=40)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--scope-tag", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    result = run_benchmark(args.chunks, args.queries, args.top_k, args.scope_tag)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
