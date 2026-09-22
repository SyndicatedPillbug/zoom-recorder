#!/usr/bin/env python3
"""Local, user-confirmed voice-profile storage and matching.

Profiles contain aggregate acoustic embeddings, not recordings.  A profile is
never created from an automatic guess: callers must pass a label that came
from an explicit user correction.  Matching is a confidence-bearing hint for
diarization, never proof of a person's identity.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_PROFILE_PATH = Path.home() / ".config" / "zoom-recorder" / "voice_profiles.json"
SCHEMA_VERSION = 1
MAX_DIMENSIONS = 4096
MAX_PROFILES = 100
MAX_SESSIONS_PER_PROFILE = 100


def _vector(value: Any) -> Optional[List[float]]:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if len(value) > MAX_DIMENSIONS:
        return None
    try:
        out = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in out):
        return None
    norm = math.sqrt(sum(item * item for item in out))
    if norm <= 1e-9:
        return None
    return [item / norm for item in out]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    return sum(a * b for a, b in zip(left, right))


def _clean_label(label: Any) -> str:
    return " ".join(str(label or "").split())[:80]


class VoiceProfileStore:
    """A small atomic JSON store for explicit, local voice matches."""

    def __init__(self, path: Optional[Path] = None, threshold: float = 0.78,
                 log: Optional[Any] = None) -> None:
        self.path = Path(path or DEFAULT_PROFILE_PATH).expanduser()
        self.threshold = max(0.0, min(1.0, float(threshold)))
        self.log = log
        self._lock = threading.RLock()
        self._profiles: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        values = raw.get("profiles", []) if isinstance(raw, dict) else []
        if not isinstance(values, list):
            return
        for item in values[:MAX_PROFILES]:
            if not isinstance(item, dict):
                continue
            profile_id = str(item.get("profile_id") or "").strip()
            label = _clean_label(item.get("label"))
            embedding = _vector(item.get("embedding"))
            if not profile_id or not label or embedding is None:
                continue
            try:
                samples = max(1, int(item.get("samples") or 1))
            except (TypeError, ValueError):
                samples = 1
            raw_sessions = item.get("sessions") or []
            if not isinstance(raw_sessions, (list, tuple)):
                raw_sessions = []
            self._profiles[profile_id] = {
                "profile_id": profile_id,
                "label": label,
                "embedding": embedding,
                "samples": samples,
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
                "sessions": [str(s) for s in raw_sessions][-MAX_SESSIONS_PER_PROFILE:],
            }

    def _save_locked(self) -> bool:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "profiles": list(self._profiles.values()),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            fd, temp_name = tempfile.mkstemp(prefix="voice-profiles-",
                                              suffix=".tmp", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
                os.chmod(temp_name, 0o600)
                os.replace(temp_name, self.path)
                os.chmod(self.path, 0o600)
            finally:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            return True
        except OSError as exc:
            if self.log:
                self.log("voice profiles not saved ({}): {}".format(self.path, exc))
            return False

    def profiles(self) -> List[Dict[str, Any]]:
        """Return metadata without embeddings, suitable for UI/diagnostics."""
        with self._lock:
            return [{key: value for key, value in profile.items() if key != "embedding"}
                    for profile in self._profiles.values()]

    def match(self, embedding: Iterable[float], threshold: Optional[float] = None
              ) -> Optional[Dict[str, Any]]:
        candidate = _vector(list(embedding))
        if candidate is None:
            return None
        limit = self.threshold if threshold is None else max(0.0, min(1.0, float(threshold)))
        best: Optional[Tuple[float, Dict[str, Any]]] = None
        second = -1.0
        with self._lock:
            for profile in self._profiles.values():
                similarity = _cosine(candidate, profile["embedding"])
                if best is None or similarity > best[0]:
                    if best is not None:
                        second = max(second, best[0])
                    best = (similarity, profile)
                else:
                    second = max(second, similarity)
        if best is None or best[0] < limit:
            return None
        similarity, profile = best
        # A close second match is intentionally reported as less certain. The
        # caller can keep the generic diarizer label when the margin is small.
        margin = max(0.0, similarity - second) if second >= 0 else similarity
        confidence = max(0.0, min(1.0, 0.5 * similarity + 0.5 * min(1.0, margin * 4)))
        return {
            "profile_id": profile["profile_id"],
            "label": profile["label"],
            "similarity": round(similarity, 4),
            "margin": round(margin, 4),
            "confidence": round(confidence, 4),
            "samples": profile["samples"],
        }

    def enroll(self, label: str, embedding: Iterable[float], session_id: str,
               profile_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Enroll or update a profile after an explicit manual label."""
        clean = _clean_label(label)
        candidate = _vector(list(embedding))
        if not clean or candidate is None:
            return None
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        sid = str(session_id or "")[:160]
        with self._lock:
            target = None
            if profile_id and profile_id in self._profiles:
                target = self._profiles[profile_id]
            if target is None:
                for profile in self._profiles.values():
                    if profile["label"].casefold() == clean.casefold():
                        target = profile
                        break
            if target is None:
                if len(self._profiles) >= MAX_PROFILES:
                    return None
                target = {
                    "profile_id": "voice:{}".format(uuid.uuid4().hex[:16]),
                    "label": clean,
                    "embedding": candidate,
                    "samples": 1,
                    "created_at": now,
                    "updated_at": now,
                    "sessions": [sid] if sid else [],
                }
                self._profiles[target["profile_id"]] = target
            else:
                if len(target["embedding"]) != len(candidate):
                    return None
                count = max(1, int(target["samples"]))
                target["embedding"] = _vector(
                    [((old * count) + new) / (count + 1)
                     for old, new in zip(target["embedding"], candidate)]) or target["embedding"]
                target["label"] = clean
                target["samples"] = count + 1
                target["updated_at"] = now
                if sid and sid not in target["sessions"]:
                    target["sessions"] = (target["sessions"] + [sid])[-MAX_SESSIONS_PER_PROFILE:]
            saved = self._save_locked()
            if not saved:
                return None
            return {key: value for key, value in target.items() if key != "embedding"}

    def forget(self, profile_id: str) -> bool:
        with self._lock:
            if str(profile_id) not in self._profiles:
                return False
            del self._profiles[str(profile_id)]
            return self._save_locked()
