"""Durable recording-integrity metadata.

This module deliberately does not decide whether audio is useful by looking only at
whether a WAV exists.  The recorder supplies per-source verification evidence and
this module writes it atomically so a crash cannot leave a plausible-looking JSON
file half written.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON durably and expose it under its final name only when complete."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{}{}.tmp".format(path.name, os.getpid()))
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Directory fsync is not available on every filesystem.  The file
            # itself was still flushed and atomically renamed.
            pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _json_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def source_record(
    *,
    expected: bool,
    segments: Iterable[Path],
    merged: Optional[Path],
    verification: Optional[Any],
    selected_device: Optional[str] = None,
    reason: str = "",
    session_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Convert capture and verification evidence into an honest source state."""
    segment_list = [Path(segment) for segment in segments]
    def display_path(segment: Path) -> str:
        if session_root is not None:
            try:
                return str(segment.relative_to(session_root))
            except ValueError:
                pass
        return str(segment)

    entry: Dict[str, Any] = {
        "expected": bool(expected),
        "state": "not_requested" if not expected else "unknown",
        "selected_device": selected_device,
        "segment_count": len(segment_list),
        "segments": [display_path(segment) for segment in segment_list],
        "artifact": str(merged) if merged is not None else None,
        "reason": reason or None,
    }
    if not expected:
        return entry
    if not segment_list:
        entry["state"] = "unavailable"
        entry["reason"] = reason or "no raw segments were found"
        return entry
    if merged is None or not merged.is_file() or verification is None:
        entry["state"] = "failed"
        entry["reason"] = reason or "source could not be merged and verified"
        return entry

    duration = _json_number(getattr(verification, "duration_s", None))
    expected_duration = _json_number(getattr(verification, "expected_duration_s", None))
    coverage = _json_number(getattr(verification, "coverage_pct", None))
    entry.update({
        "duration_s": duration,
        "expected_duration_s": expected_duration,
        "mean_db": _json_number(getattr(verification, "mean_db", None)),
        "max_db": _json_number(getattr(verification, "max_db", None)),
        "coverage_pct": coverage,
        "long_silences": [
            {"start_s": float(start), "duration_s": float(length)}
            for start, length in (getattr(verification, "long_silences", None) or [])
        ],
    })
    if duration is None or duration <= 0:
        entry["state"] = "failed"
        entry["reason"] = reason or "merged artifact has no readable duration"
    elif coverage is not None and coverage <= 0:
        entry["state"] = "silent"
        entry["reason"] = reason or "merged artifact contains no meaningful signal"
    elif expected_duration and duration < max(2.0, expected_duration * 0.98):
        entry["state"] = "truncated"
        entry["reason"] = reason or "merged duration is shorter than raw segment duration"
    elif coverage is not None and coverage < 50.0:
        entry["state"] = "degraded"
        entry["reason"] = reason or "source contains substantial silence"
    else:
        entry["state"] = "captured"
    return entry


def build_report(
    *,
    session: Path,
    state: str,
    expected_sources: Mapping[str, bool],
    sources: Mapping[str, Mapping[str, Any]],
    phase: str,
    reason: str = "",
    started_at: Optional[str] = None,
    routing: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the stable, redacted session-level integrity report."""
    return {
        "schema_version": SCHEMA_VERSION,
        "session": Path(session).name,
        "updated_at": _now(),
        "started_at": started_at,
        "phase": phase,
        "state": state,
        "reason": reason or None,
        "expected_sources": dict(expected_sources),
        "sources": {name: dict(value) for name, value in sources.items()},
    }
    if routing is not None:
        report["routing"] = dict(routing)


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    atomic_write_json(Path(path), report)
