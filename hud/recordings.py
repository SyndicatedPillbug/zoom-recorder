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


def list_recordings(basedir: str, limit: int = 200,
                    probe: bool = True) -> List[Recording]:
    """Newest-first list of recording sessions under ``basedir``."""
    root = Path(basedir).expanduser()
    if not root.is_dir():
        return []
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
                rec.duration_s = _probe_duration(source)
            found.append(rec)
            if len(found) >= limit:
                return found
    return found
