"""Secret-free diagnostics exports for the Control Center.

Diagnostics must help explain setup failures without becoming a second way to
export transcript text, API keys, or raw audio.  The report is therefore made
only from checks, safe status fields, and non-secret configuration metadata.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def _display_path(value: str) -> str:
    home = str(Path.home())
    text = str(value)
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def _safe_value(value: Any, key: str = "") -> Any:
    if key == "api_keys":
        if isinstance(value, dict):
            return {str(name): bool(secret) for name, secret in value.items()}
        return {}
    if isinstance(value, dict):
        return {str(name): _safe_value(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_safe_value(item, key) for item in value]
    if isinstance(value, str) and any(token in key.lower() for token in
                                     ("dir", "path", "basedir", "model")):
        return _display_path(value)
    return value


def build_report(checks: Iterable[Dict[str, Any]], status: Dict[str, Any],
                 config: Dict[str, Any], config_path: Optional[str] = None) -> Dict[str, Any]:
    """Build a redacted report with no transcript/audio/provider secret fields."""
    safe_status = {
        key: _safe_value(status.get(key))
        for key in (
            "recording", "routing_active", "volume", "mode", "onboarded",
            "offline", "stt_backend", "answers_enabled", "transcription_model",
            "diarization", "api_keys_set",
        ) if key in status
    }
    safe_checks = []
    for check in checks:
        safe_checks.append({
            key: _safe_value(check.get(key))
            for key in ("name", "ok", "detail", "fix", "critical")
            if key in check
        })
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": safe_status,
        "checks": safe_checks,
        "config": _safe_value(config),
        "config_path": _display_path(config_path) if config_path else None,
    }


def write_report(path: Path, report: Dict[str, Any]) -> Path:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.replace(str(temporary), str(path))
    return path


def default_path(now: Optional[datetime] = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return Path.home() / "Library" / "Logs" / "zoom-recorder" / \
        ("diagnostics-{}.json".format(stamp))
