#!/usr/bin/env python3
"""Human-readable, evidence-backed identity for a recording session.

The recorder's short hexadecimal suffix remains the collision-safe identity.
This module adds a readable topic slug and a sidecar record without relying on
another provider call or trusting an arbitrary filename.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_STAMP_RE = re.compile(r"^\s*\[[^]]+\]\s*")
_SPEAKER_RE = re.compile(r"^([^:]{1,80}):\s*")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'&+./-]*")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FILLER_RE = re.compile(
    r"^(?:hello|hi|hey|good morning|good afternoon|good evening|"
    r"thanks for joining|thank you for joining|how are you|can you hear me)\b",
    re.I,
)
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can",
    "do", "for", "from", "good", "have", "how", "i", "in", "is", "it",
    "me", "my", "of", "on", "or", "our", "that", "the", "this", "to",
    "was", "we", "what", "when", "where", "which", "who", "why", "with",
    "would", "you", "your",
}


def _transcript_lines(transcript: str) -> Iterable[Tuple[str, str]]:
    for raw in (transcript or "").splitlines():
        line = _STAMP_RE.sub("", raw).strip()
        if not line:
            continue
        speaker = ""
        match = _SPEAKER_RE.match(line)
        if match:
            speaker = match.group(1).strip().strip("*_")
            line = line[match.end():].strip()
        yield speaker, line


def _words(text: str) -> List[str]:
    return _WORD_RE.findall(text or "")


def derive_title(transcript: str, fallback: str = "Meeting") -> Tuple[str, str]:
    """Return ``(title, evidence_line)`` from the earliest useful speech.

    The title is intentionally conservative: the first substantive sentence
    is more auditable than an opaque generated label. A later AI summary can
    refine the title, but the initial identity never needs network access.
    """
    candidates: List[str] = []
    for _speaker, line in _transcript_lines(transcript):
        if len(_words(line)) < 4 or _FILLER_RE.match(line):
            continue
        candidates.append(line.strip())
    if not candidates:
        return fallback.strip() or "Meeting", ""
    evidence = candidates[0]
    words = _words(evidence)
    title_words = words[:10]
    title = " ".join(title_words).strip(" .,;:!?-_")
    if len(words) > len(title_words):
        title += "…"
    return title or fallback.strip() or "Meeting", evidence[:300]


def slugify(text: str, fallback: str = "meeting", max_chars: int = 64) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_RE.sub("-", ascii_text).strip("-")
    return (slug[:max_chars].rstrip("-") or fallback).strip("-")


def _participants(transcript: str) -> List[str]:
    found: List[str] = []
    for speaker, _line in _transcript_lines(transcript):
        if speaker and speaker not in found:
            found.append(speaker)
    return found[:12]


def build_identity(started_at: datetime, ended_at: Optional[datetime],
                   original_folder: str, transcript: str, cfg: Any) -> Dict[str, Any]:
    fallback = "Meeting {}".format(started_at.strftime("%Y-%m-%d %H:%M"))
    title, evidence = derive_title(transcript, fallback=fallback)
    return {
        "schema_version": 1,
        "session_id": original_folder,
        "started_at": started_at.isoformat(timespec="seconds"),
        "ended_at": ended_at.isoformat(timespec="seconds") if ended_at else None,
        "title": title,
        "slug": slugify(title),
        "title_source": "first_substantive_transcript_line" if evidence else "timestamp_fallback",
        "title_evidence": evidence,
        "title_evidence_sha256": hashlib.sha256(evidence.encode("utf-8")).hexdigest()
        if evidence else None,
        "participants": _participants(transcript),
        "transcript_words": len(_words(transcript)),
        "recording_mode": (
            "microphone + other party" if getattr(cfg, "record_mic", True)
            and getattr(cfg, "use_system", True) else
            "microphone only" if getattr(cfg, "record_mic", True) else
            "other party only"),
        "stt_backend": getattr(cfg, "stt_backend", None),
        "stt_model": getattr(cfg, "stt_model", None),
        "answer_backend": getattr(cfg, "answers_backend", None),
        "answer_model": getattr(cfg, "chat_model", None),
        "original_folder": original_folder,
    }


def meaningful_folder_name(original: str, identity: Dict[str, Any]) -> str:
    """Insert a readable slug while retaining the original collision suffix."""
    if not identity.get("slug") or "_" not in original:
        return original
    clock, suffix = original.split("_", 1)
    return "{}_{}_{}".format(clock, identity["slug"], suffix)


def write_identity(path: Path, identity: Dict[str, Any]) -> None:
    path.write_text(json.dumps(identity, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
