#!/usr/bin/env python3
"""Run the local rolling benchmark across a versioned fixture manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .audio_benchmark import run_benchmark
from .benchmark_manifest import ManifestError, load_manifest, write_result


SUITE_SCHEMA_VERSION = 1


def run_suite(manifest: Path, model: Path, asset_root: Optional[Path] = None,
              window_seconds: float = 4.0, interval_seconds: float = 0.8,
              pace: bool = True, require_files: bool = True,
              runner: Callable[..., Dict[str, object]] = run_benchmark) -> Dict[str, Any]:
    specs = load_manifest(manifest, require_files=require_files, asset_root=asset_root)
    results: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for spec in specs:
        if not spec.audio_exists:
            skipped.append({"fixture_id": spec.fixture_id, "reason": "audio_missing",
                            "audio": str(spec.audio)})
            continue
        if spec.reference is not None and not spec.reference_exists:
            skipped.append({"fixture_id": spec.fixture_id, "reason": "reference_missing",
                            "reference": str(spec.reference)})
            continue
        result = dict(runner(
            spec.audio, model, reference=spec.reference,
            window_seconds=window_seconds, interval_seconds=interval_seconds, pace=pace))
        result.update({
            "fixture_id": spec.fixture_id,
            "fixture_kind": spec.kind,
            "fixture_duration_seconds": spec.duration_seconds,
            "fixture_speech_bounds": list(spec.speech_bounds),
            "fixture_source_url": spec.source_url,
        })
        results.append(result)
    return {
        "suite_schema_version": SUITE_SCHEMA_VERSION,
        "manifest": str(Path(manifest).expanduser().resolve()),
        "model": str(Path(model).expanduser().resolve()),
        "window_seconds": window_seconds,
        "interval_seconds": interval_seconds,
        "paced": pace,
        "fixture_count": len(specs),
        "completed_count": len(results),
        "skipped_count": len(skipped),
        "results": results,
        "skipped": skipped,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a manifest-driven local STT benchmark")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path,
                        help="directory containing external audio assets")
    parser.add_argument("--window-seconds", type=float, default=4.0)
    parser.add_argument("--interval-seconds", type=float, default=0.8)
    parser.add_argument("--no-pace", action="store_true")
    parser.add_argument("--allow-missing", action="store_true",
                        help="report missing fixtures as skipped instead of failing")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_suite(
            args.manifest, args.model, asset_root=args.asset_root,
            window_seconds=args.window_seconds, interval_seconds=args.interval_seconds,
            pace=not args.no_pace, require_files=not args.allow_missing,
        )
    except ManifestError as exc:
        print("benchmark manifest error: {}".format(exc), file=sys.stderr)
        return 2
    if args.output:
        write_result(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
