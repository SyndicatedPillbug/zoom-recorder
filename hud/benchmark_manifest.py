#!/usr/bin/env python3
"""Versioned benchmark-fixture manifests and result persistence.

The manifest describes external audio by a relative path and source metadata.
Large or licensed recordings do not belong in the repository, so loading a
manifest distinguishes a structurally valid entry from a locally ready file.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


MANIFEST_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1


class ManifestError(ValueError):
    """Raised when a benchmark manifest is invalid or incomplete."""


@dataclass(frozen=True)
class FixtureSpec:
    fixture_id: str
    audio: Path
    reference: Optional[Path]
    kind: str
    duration_seconds: Optional[float]
    speech_bounds: tuple[float, ...]
    source_url: Optional[str]
    notes: str
    audio_exists: bool
    reference_exists: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.fixture_id,
            "audio": str(self.audio),
            "reference": str(self.reference) if self.reference else None,
            "kind": self.kind,
            "duration_seconds": self.duration_seconds,
            "speech_bounds": list(self.speech_bounds),
            "source_url": self.source_url,
            "notes": self.notes,
            "audio_exists": self.audio_exists,
            "reference_exists": self.reference_exists,
        }


def _optional_float(value: Any, label: str) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ManifestError("{} must be a number or null".format(label)) from exc
    if number < 0:
        raise ManifestError("{} must not be negative".format(label))
    return number


def _resolve(root: Path, value: Any, label: str, required: bool = True) -> Optional[Path]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ManifestError("{} must be a non-empty relative path".format(label))
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ManifestError("{} must stay inside the manifest directory".format(label))
    return (root / candidate).resolve()


def load_manifest(path: Path, require_files: bool = False,
                  asset_root: Optional[Path] = None) -> List[FixtureSpec]:
    """Load and validate a manifest without requiring external audio by default."""
    manifest_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError("could not read manifest {}: {}".format(manifest_path, exc)) from exc
    if not isinstance(payload, dict):
        raise ManifestError("manifest root must be an object")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError("unsupported manifest schema_version")
    entries = payload.get("fixtures")
    if not isinstance(entries, list) or not entries:
        raise ManifestError("manifest fixtures must be a non-empty list")

    root = manifest_path.parent
    audio_root = Path(asset_root).expanduser().resolve() if asset_root else root
    seen = set()
    specs: List[FixtureSpec] = []
    for index, entry in enumerate(entries):
        label = "fixtures[{}]".format(index)
        if not isinstance(entry, dict):
            raise ManifestError("{} must be an object".format(label))
        fixture_id = str(entry.get("id") or "").strip()
        if not fixture_id or fixture_id in seen:
            raise ManifestError("{} has a missing or duplicate id".format(label))
        seen.add(fixture_id)
        audio = _resolve(audio_root, entry.get("audio"), label + ".audio")
        reference = _resolve(root, entry.get("reference"), label + ".reference", required=False)
        kind = str(entry.get("kind") or "speech").strip().lower()
        duration = _optional_float(entry.get("duration_seconds"), label + ".duration_seconds")
        raw_bounds = entry.get("speech_bounds", [])
        if not isinstance(raw_bounds, list) or len(raw_bounds) % 2:
            raise ManifestError("{} speech_bounds must contain start/end pairs".format(label))
        try:
            bounds = tuple(float(value) for value in raw_bounds)
        except (TypeError, ValueError) as exc:
            raise ManifestError("{} speech_bounds must contain numbers".format(label)) from exc
        if any(value < 0 for value in bounds):
            raise ManifestError("{} speech_bounds must not be negative".format(label))
        if any(right < left for left, right in zip(bounds[::2], bounds[1::2])):
            raise ManifestError("{} speech_bounds must have start <= end".format(label))
        if duration is not None and any(value > duration for value in bounds):
            raise ManifestError("{} speech_bounds must fit duration_seconds".format(label))
        source_url = entry.get("source_url")
        if source_url is not None and not isinstance(source_url, str):
            raise ManifestError("{} source_url must be a string or null".format(label))
        notes = str(entry.get("notes") or "").strip()
        audio_exists = bool(audio and audio.is_file())
        reference_exists = reference is None or reference.is_file()
        if require_files and not audio_exists:
            raise ManifestError("{} audio is missing: {}".format(fixture_id, audio))
        if require_files and not reference_exists:
            raise ManifestError("{} reference is missing: {}".format(fixture_id, reference))
        specs.append(FixtureSpec(
            fixture_id=fixture_id, audio=audio, reference=reference,
            kind=kind, duration_seconds=duration, speech_bounds=bounds,
            source_url=source_url, notes=notes, audio_exists=audio_exists,
            reference_exists=reference_exists,
        ))
    return specs


def write_result(path: Path, result: Dict[str, Any]) -> None:
    """Atomically persist a benchmark result without partial JSON."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(result)
    payload.setdefault("result_schema_version", RESULT_SCHEMA_VERSION)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    os.replace(str(temp), str(target))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a meeting audio benchmark manifest")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--require-files", action="store_true",
                        help="fail when referenced local audio or references are missing")
    parser.add_argument("--asset-root", type=Path,
                        help="optional directory containing external audio assets")
    args = parser.parse_args(argv)
    specs = load_manifest(args.manifest, require_files=args.require_files,
                          asset_root=args.asset_root)
    print(json.dumps({"schema_version": MANIFEST_SCHEMA_VERSION,
                      "fixtures": [spec.as_dict() for spec in specs]},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
