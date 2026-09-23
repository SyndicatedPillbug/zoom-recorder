#!/usr/bin/env python3
"""Persistent meeting-workspace data and revision-safe transcript editing.

The recorder owns the original capture artifacts.  This module only reads those
artifacts and writes a separate derived interpretation layer, so correcting a
speaker or a word can never damage the evidence needed to review the meeting.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .recordings import DAY_RE, LEGACY_SESSION_RE, SESSION_RE

SCHEMA_VERSION = 1
TIMESTAMP_LINE = re.compile(
    r"^\s*\[(?P<stamp>\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)\]\s*"
    r"(?:(?P<speaker>[^:]{1,120}):\s*)?(?P<text>.*)\s*$"
)


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)


def _json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_session(raw_path: str, basedir: str) -> Optional[Path]:
    """Resolve a user-selected session while preventing path escape."""
    if not raw_path:
        return None
    root = Path(basedir).expanduser().resolve()
    try:
        candidate = Path(raw_path).expanduser().resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    if not candidate.is_dir() or not DAY_RE.match(candidate.parent.name):
        return None
    if not (SESSION_RE.match(candidate.name) or LEGACY_SESSION_RE.match(candidate.name)):
        return None
    return candidate


def _seconds(stamp: str) -> float:
    parts = stamp.split(":")
    try:
        hours, minutes = int(parts[0]), int(parts[1])
        seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    except (IndexError, ValueError):
        return 0.0


def _speaker_id(label: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    if not normalized:
        return "unknown"
    if normalized in {"you", "me", "odin"}:
        return "local"
    if normalized in {"others", "other", "other-party", "other-person"}:
        return "others"
    return normalized[:64]


def _parse_transcript(path: Path) -> Tuple[List[Dict[str, Any]], str]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], ""
    lines = raw.splitlines()
    parsed: List[Tuple[Optional[float], str, str, int]] = []
    for line_number, line in enumerate(lines):
        if not line.strip() or line.strip() in {"[BLANK_AUDIO]", "[inaudible]"}:
            continue
        match = TIMESTAMP_LINE.match(line)
        if match:
            speaker = (match.group("speaker") or "unknown").strip()
            text = match.group("text").strip()
            stamp = _seconds(match.group("stamp"))
        else:
            speaker, text, stamp = "unknown", line.strip(), None
        if text:
            parsed.append((stamp, speaker, text, line_number))
    first_stamp = next((item[0] for item in parsed if item[0] is not None), None)
    segments: List[Dict[str, Any]] = []
    for index, (stamp, speaker, text, line_number) in enumerate(parsed):
        start = None if stamp is None or first_stamp is None else max(0.0, stamp - first_stamp)
        if start is None:
            start = float(index * 3)
        if index + 1 < len(parsed) and parsed[index + 1][0] is not None and stamp is not None:
            end = max(start + 0.2, parsed[index + 1][0] - first_stamp)  # type: ignore[operator]
        else:
            end = start + max(2.0, min(12.0, len(text.split()) / 2.5))
        segment_id = hashlib.sha256(
            "{}:{}:{}".format(path.name, line_number, text).encode("utf-8")
        ).hexdigest()[:20]
        sid = _speaker_id(speaker)
        segments.append({
            "segment_id": segment_id,
            "start_s": round(start, 3),
            "end_s": round(end, 3),
            "speaker_id": sid,
            "speaker_label": speaker if speaker != "unknown" else "Unknown",
            "text": text,
            "text_source": "live" if "live_transcript" in path.name else "original",
            "speaker_source": "channel" if sid not in {"unknown"} else "unknown",
            "confidence": None,
            "audio_source": "mixed",
            "revision": 0,
        })
    return segments, path.name


def _transcript_source(session: Path) -> Optional[Path]:
    for candidate in (session / "derived" / "live_transcript.txt", session / "transcript.txt"):
        if candidate.is_file():
            return candidate
    return None


def _workspace_path(session: Path) -> Path:
    return session / "derived" / "transcript_workspace.json"


def _revision_path(session: Path, revision: int) -> Path:
    return session / "derived" / "transcript_revisions" / "revision-{:06d}.json".format(revision)


def _load_or_bootstrap(session: Path) -> Dict[str, Any]:
    current = _json(_workspace_path(session))
    if current.get("schema_version") and isinstance(current.get("segments"), list):
        return current
    source = _transcript_source(session)
    segments, source_name = _parse_transcript(source) if source else ([], "")
    speakers = _speaker_catalog(segments)
    current = {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "updated_at": None,
        "source": source_name,
        "speakers": speakers,
        "segments": segments,
        "history": [],
    }
    return current


def _speaker_catalog(segments: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    for segment in segments:
        sid = str(segment.get("speaker_id") or "unknown")
        label = str(segment.get("speaker_label") or "Unknown")
        found.setdefault(sid, {"speaker_id": sid, "label": label, "source": segment.get("speaker_source") or "unknown"})
    if "unknown" not in found:
        found["unknown"] = {"speaker_id": "unknown", "label": "Unknown", "source": "unknown"}
    return list(found.values())


def _integrity_status(report: Dict[str, Any], session: Path) -> Dict[str, Any]:
    if not report:
        return {"state": "unknown", "label": "Integrity not yet recorded", "detail": "Older session; verify its audio before relying on it."}
    state = str(report.get("state") or report.get("overall") or "unknown").lower()
    if isinstance(report.get("summary"), dict):
        state = str(report["summary"].get("state") or state).lower()
    labels = {
        "healthy": "Healthy", "partial": "Partial", "degraded": "Degraded",
        "recovered": "Recovered", "capture_failed": "Capture failed", "failed": "Capture failed",
    }
    return {"state": state, "label": labels.get(state, "Review capture"),
            "detail": "Audio and transcript provenance are available."}


def _media(session: Path) -> List[Dict[str, Any]]:
    entries = []
    for label, filename in (("mixed", "derived/recording_mixed.wav"),
                            ("microphone", "recording_mic.wav"),
                            ("other party", "recording_sys.wav")):
        path = session / filename
        if path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            entries.append({"kind": label, "path": str(path), "bytes": size})
    return entries


def load_session(session: Path) -> Dict[str, Any]:
    identity = _json(session / "session.json")
    report = _json(session / "recording_integrity.json")
    workspace = _load_or_bootstrap(session)
    return {
        "ok": True,
        "path": str(session),
        "name": session.name,
        "identity": identity,
        "integrity": report,
        "health": _integrity_status(report, session),
        "media": _media(session),
        "workspace": workspace,
        "raw_transcript": str(_transcript_source(session)) if _transcript_source(session) else None,
    }


def save_workspace(session: Path, incoming: Dict[str, Any]) -> Dict[str, Any]:
    current = _load_or_bootstrap(session)
    expected = incoming.get("expected_revision")
    actual = int(current.get("revision") or 0)
    if expected is not None and int(expected) != actual:
        return {"ok": False, "conflict": True, "error": "This meeting changed elsewhere. Reload it before saving.",
                "revision": actual}
    segments = incoming.get("segments")
    speakers = incoming.get("speakers")
    if not isinstance(segments, list) or not isinstance(speakers, list):
        return {"ok": False, "error": "A valid transcript and speaker list are required."}
    previous_by_id = {
        str(item.get("segment_id")): item for item in current.get("segments", [])
        if isinstance(item, dict)
    }
    clean_segments = []
    for item in segments:
        if not isinstance(item, dict) or not str(item.get("segment_id") or "").strip():
            return {"ok": False, "error": "Every transcript row needs a stable segment ID."}
        clean = dict(item)
        clean["segment_id"] = str(clean["segment_id"])
        clean["text"] = str(clean.get("text") or "").strip()
        clean["speaker_id"] = str(clean.get("speaker_id") or "unknown")
        clean["speaker_label"] = str(clean.get("speaker_label") or "Unknown")
        previous = previous_by_id.get(clean["segment_id"], {})
        text_changed = clean["text"] != str(previous.get("text") or "")
        speaker_changed = (clean["speaker_id"] != str(previous.get("speaker_id") or "unknown") or
                           clean["speaker_label"] != str(previous.get("speaker_label") or "Unknown"))
        clean["revision"] = int(previous.get("revision") or clean.get("revision") or 0)
        if text_changed:
            clean["revision"] += 1
            clean["text_source"] = "user"
        else:
            clean["text_source"] = previous.get("text_source") or clean.get("text_source") or "live"
        if speaker_changed:
            clean["revision"] = max(clean["revision"] + 1, int(previous.get("revision") or 0) + 1)
            clean["speaker_source"] = "user"
        else:
            clean["speaker_source"] = previous.get("speaker_source") or clean.get("speaker_source") or "unknown"
        clean_segments.append(clean)
    clean_speakers = []
    for item in speakers:
        if isinstance(item, dict) and str(item.get("speaker_id") or "").strip():
            clean_speakers.append({
                "speaker_id": str(item["speaker_id"]),
                "label": str(item.get("label") or "Unknown").strip() or "Unknown",
                "source": str(item.get("source") or "user"),
            })
    now = datetime.now(timezone.utc).isoformat()
    revision = actual + 1
    if actual == 0 and not _revision_path(session, 0).is_file():
        _atomic_json(_revision_path(session, 0), deepcopy(current))
    updated = {"schema_version": SCHEMA_VERSION, "revision": revision, "updated_at": now,
               "source": current.get("source"), "speakers": clean_speakers,
               "segments": clean_segments, "history": list(current.get("history") or [])[-19:]}
    history_item = {"revision": revision, "updated_at": now, "segment_count": len(clean_segments),
                    "edited_by": "user", "previous_revision": actual}
    updated["history"].append(history_item)
    _atomic_json(_revision_path(session, revision), updated)
    _atomic_json(_workspace_path(session), updated)
    return {"ok": True, "revision": revision, "workspace": updated}


def restore_workspace_revision(session: Path, target_revision: int,
                               expected_revision: Optional[int] = None) -> Dict[str, Any]:
    """Restore a prior derived revision as a new revision; never rewrite raw data."""
    current = _load_or_bootstrap(session)
    actual = int(current.get("revision") or 0)
    if expected_revision is not None and int(expected_revision) != actual:
        return {"ok": False, "conflict": True, "error": "This meeting changed elsewhere. Reload it before undoing.",
                "revision": actual}
    if target_revision < 0 or target_revision >= actual:
        return {"ok": False, "error": "No earlier saved revision is available."}
    target = _json(_revision_path(session, target_revision))
    if not target or not isinstance(target.get("segments"), list):
        return {"ok": False, "error": "That revision is no longer available."}
    now = datetime.now(timezone.utc).isoformat()
    revision = actual + 1
    restored = {
        "schema_version": SCHEMA_VERSION, "revision": revision, "updated_at": now,
        "source": current.get("source"), "speakers": target.get("speakers", []),
        "segments": target.get("segments", []),
        "history": list(current.get("history") or [])[-19:] + [{
            "revision": revision, "updated_at": now, "segment_count": len(target.get("segments", [])),
            "edited_by": "user-undo", "restored_from": target_revision,
            "previous_revision": actual,
        }],
    }
    _atomic_json(_revision_path(session, revision), restored)
    _atomic_json(_workspace_path(session), restored)
    return {"ok": True, "revision": revision, "workspace": restored}


def export_workspace(session: Path, fmt: str = "markdown") -> Dict[str, Any]:
    workspace = _load_or_bootstrap(session)
    revision = int(workspace.get("revision") or 0)
    if fmt not in {"markdown", "txt", "json"}:
        return {"ok": False, "error": "unsupported export format"}
    if fmt == "json":
        destination = session / "derived" / "transcript_edited.json"
        payload = workspace
    else:
        suffix = "md" if fmt == "markdown" else "txt"
        destination = session / "derived" / ("transcript_edited." + suffix)
        lines = []
        identity = _json(session / "session.json")
        lines.append("# {}".format(identity.get("title") or session.name))
        lines.append("")
        for segment in workspace.get("segments", []):
            label = segment.get("speaker_label") or "Unknown"
            start = float(segment.get("start_s") or 0)
            lines.append("[{:02d}:{:02d}] {}: {}".format(int(start // 60), int(start % 60), label, segment.get("text") or ""))
        payload = {"text": "\n".join(lines) + "\n"}
    if fmt == "json":
        _atomic_json(destination, payload)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload["text"], encoding="utf-8")
    return {"ok": True, "path": str(destination), "revision": revision}
