#!/usr/bin/env python3
"""Read-only listing of past recordings for the Control Center.

Sessions live under ``<basedir>/<YYYY-MM-DD>/<HH-MM-SS>_<id>/`` (see
zoom_record.py). This module only inspects them -- it never writes or deletes
anything.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SESSION_RE = re.compile(r"^\d{2}-\d{2}-\d{2}_[0-9a-f]+$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Recording:
    path: str
    day: str
    name: str
    started: str                       # "YYYY-MM-DD HH:MM:SS" (best effort)
    duration_s: Optional[float] = None
    has_mic: bool = False
    has_system: bool = False
    transcript: Optional[str] = None
    summary: Optional[str] = None
    mixed: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "day": self.day,
            "name": self.name,
            "started": self.started,
            "duration_s": self.duration_s,
            "duration": format_duration(self.duration_s),
            "has_mic": self.has_mic,
            "has_system": self.has_system,
            "transcript": self.transcript,
            "summary": self.summary,
            "mixed": self.mixed,
        }


def format_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "--:--"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "{}:{:02d}:{:02d}".format(hours, minutes, secs)
    return "{}:{:02d}".format(minutes, secs)


def _probe_duration(path: Path) -> Optional[float]:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15)
        return float(proc.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _session_started(day: str, name: str) -> str:
    # "17-27-11_ab12cd34" -> "YYYY-MM-DD 17:27:11"
    time_part = name.split("_", 1)[0].replace("-", ":")
    return "{} {}".format(day, time_part)


def _load_index(basedir: Path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads((basedir / ".recordings_index.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(basedir: Path, index: Dict[str, Dict[str, Any]]) -> None:
    try:
        (basedir / ".recordings_index.json").write_text(
            json.dumps(index, indent=0), encoding="utf-8")
    except OSError:
        pass


def list_recordings(basedir: str, limit: int = 200,
                    probe: bool = True) -> List[Recording]:
    """Newest-first list of recording sessions under ``basedir``.

    Durations are cached by (path, mtime) in ``.recordings_index.json`` so the
    list does not spawn ffprobe for every session on every open.
    """
    root = Path(basedir).expanduser()
    if not root.is_dir():
        return []
    index = _load_index(root) if probe else {}
    dirty = False
    found: List[Recording] = []
    for day_dir in sorted(root.iterdir(), reverse=True):
        if not day_dir.is_dir() or not DAY_RE.match(day_dir.name):
            continue
        for session in sorted(day_dir.iterdir(), reverse=True):
            if not session.is_dir() or not SESSION_RE.match(session.name):
                continue
            rec = Recording(path=str(session), day=day_dir.name, name=session.name,
                            started=_session_started(day_dir.name, session.name))
            mic = session / "recording_mic.wav"
            syswav = session / "recording_sys.wav"
            mixed = session / "derived" / "recording_mixed.wav"
            transcript = session / "transcript.txt"
            summary = session / "derived" / "live_summary.md"
            rec.has_mic = mic.is_file()
            rec.has_system = syswav.is_file()
            rec.mixed = str(mixed) if mixed.is_file() else None
            rec.transcript = str(transcript) if transcript.is_file() else None
            rec.summary = str(summary) if summary.is_file() else None
            source = mixed if mixed.is_file() else (mic if mic.is_file() else syswav)
            if probe and source.is_file():
                try:
                    mtime = source.stat().st_mtime
                except OSError:
                    mtime = 0
                cached = index.get(str(session))
                if cached and cached.get("mtime") == mtime:
                    rec.duration_s = cached.get("duration")
                else:
                    rec.duration_s = _probe_duration(source)
                    index[str(session)] = {"mtime": mtime, "duration": rec.duration_s}
                    dirty = True
            found.append(rec)
            if len(found) >= limit:
                if dirty:
                    _save_index(root, index)
                return found
    if dirty:
        _save_index(root, index)
    return found


def move_to_trash(session_path: str, basedir: str) -> Dict[str, Any]:
    """Move a session folder to ~/.Trash (never delete). Only paths under the
    recordings folder that look like sessions are accepted."""
    import shutil
    import time as _time

    root = Path(basedir).expanduser().resolve()
    target = Path(session_path).expanduser().resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return {"ok": False, "error": "path not allowed"}
    if not SESSION_RE.match(target.name):
        return {"ok": False, "error": "not a recording folder"}
    trash = Path.home() / ".Trash"
    trash.mkdir(exist_ok=True)
    dest = trash / "{}-{}".format(target.name, int(_time.time()))
    try:
        shutil.move(str(target), str(dest))
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "moved_to": str(dest)}
