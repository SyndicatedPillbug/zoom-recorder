#!/usr/bin/env python3
"""HUD configuration: provider presets + defaults, merged from an optional
config file and environment variables.

Precedence (lowest to highest):
    built-in defaults  <  ~/.config/zoom-recorder/config.json  <  env vars

Provider API keys are never written by this module. They are read from the
environment first, then from an optional ``api_keys`` block in the config file
(which the user should chmod 0600). Keeping only the transcript text in flight
is the goal -- see README for the privacy note.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_PATH = Path.home() / ".config" / "zoom-recorder" / "config.json"


@dataclass
class Provider:
    name: str
    base_url: str
    api_key_env: Optional[str]
    chat_model: str
    rolling_model: str
    stt_model: Optional[str] = None


# First-party Groq is the default because it is both the fastest and the
# cheapest option for STT, and cheap for chat. OpenRouter / OpenAI / Ollama are
# wired in as drop-in alternatives (all expose an OpenAI-compatible API), so the
# same client code serves every provider.
PROVIDERS: Dict[str, Provider] = {
    "groq": Provider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        chat_model="openai/gpt-oss-120b",
        rolling_model="openai/gpt-oss-20b",
        stt_model="whisper-large-v3-turbo",
    ),
    "openrouter": Provider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        chat_model="openai/gpt-4o-mini",
        rolling_model="openai/gpt-4o-mini",
        stt_model=None,
    ),
    "openai": Provider(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        chat_model="gpt-4o-mini",
        rolling_model="gpt-4o-mini",
        stt_model="whisper-1",
    ),
    "ollama": Provider(
        name="ollama",
        base_url="http://localhost:11434/v1",
        api_key_env=None,
        chat_model="llama3.1",
        rolling_model="llama3.1",
        stt_model=None,
    ),
}

# Backends that do speech-to-text locally via whisper.cpp.
LOCAL_STT_BACKENDS = {"local", "whisper"}


def get_provider(name: str) -> Provider:
    key = (name or "").strip().lower()
    if key not in PROVIDERS:
        raise KeyError("Unknown provider '{}'. Known: {}".format(name, ", ".join(sorted(PROVIDERS))))
    return PROVIDERS[key]


@dataclass
class HudConfig:
    enabled: bool = False

    # Speech-to-text
    stt_backend: str = "groq"          # groq | openai | local
    stt_model: Optional[str] = None    # None => provider default
    stt_chunk_seconds: float = 14.0    # Groq bills a 10s minimum per request
    stt_min_speech_seconds: float = 0.6
    stt_whisper_bin: str = "whisper-server"

    # Answers
    answers_enabled: bool = True
    answers_backend: str = "groq"
    answers_fallback: List[str] = field(default_factory=list)
    chat_model: Optional[str] = None
    rolling_model: Optional[str] = None
    answer_interval: float = 35.0
    rolling_enabled: bool = True
    answer_max_tokens: int = 600
    context_minutes: float = 3.0
    question_cooldown: float = 6.0

    # Knowledge base
    kb_enabled: bool = True
    kb_dirs: List[str] = field(default_factory=list)
    kb_model: str = "all-MiniLM-L6-v2"
    kb_top_k: int = 5
    kb_reindex: bool = False
    kb_cache_dir: Optional[str] = None

    # Speaker labelling (channel-based: mic vs system/loopback)
    speakers_enabled: bool = True
    self_name: str = "You"
    remote_name: str = "Others"

    # HUD server
    port: int = 0
    open_browser: bool = True
    host: str = "127.0.0.1"

    # Budget caps (0 == trust the provider's rate-limit headers)
    budget_tpm: int = 0
    budget_tpd: int = 0

    # Provider credentials loaded from the config file (env always wins)
    api_keys: Dict[str, str] = field(default_factory=dict)

    # Testing / alternate sources
    audio_file: Optional[str] = None

    def provider_for_stt(self) -> Optional[Provider]:
        name = (self.stt_backend or "").lower()
        if name in LOCAL_STT_BACKENDS:
            return None
        return get_provider(name)

    def resolve_stt_model(self) -> Optional[str]:
        if self.stt_model:
            return self.stt_model
        prov = self.provider_for_stt()
        return prov.stt_model if prov else None

    def resolve_chat_model(self) -> str:
        prov = get_provider(self.answers_backend)
        return self.chat_model or prov.chat_model

    def resolve_rolling_model(self) -> str:
        prov = get_provider(self.answers_backend)
        return self.rolling_model or prov.rolling_model

    def api_key_for(self, provider_name: str) -> Optional[str]:
        try:
            prov = get_provider(provider_name)
        except KeyError:
            return None
        if prov.api_key_env:
            env_val = os.environ.get(prov.api_key_env)
            if env_val:
                return env_val
        return self.api_keys.get(provider_name)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _defaults() -> Dict[str, Any]:
    return {
        "stt": {
            "backend": "groq",
            "model": None,
            "chunk_seconds": 14.0,
            "min_speech_seconds": 0.6,
            "whisper_bin": "whisper-server",
        },
        "answers": {
            "enabled": True,
            "backend": "groq",
            "fallback": [],
            "chat_model": None,
            "rolling_model": None,
            "interval": 35.0,
            "rolling_enabled": True,
            "max_tokens": 600,
            "context_minutes": 3.0,
            "question_cooldown": 6.0,
        },
        "kb": {
            "enabled": True,
            "dirs": [],
            "model": "all-MiniLM-L6-v2",
            "top_k": 5,
            "reindex": False,
            "cache_dir": None,
        },
        "hud": {"port": 0, "open_browser": True, "host": "127.0.0.1"},
        "speakers": {"enabled": True, "self_name": "You", "remote_name": "Others"},
        "budget": {"tpm": 0, "tpd": 0},
        "api_keys": {},
    }


def load_config(path: Optional[Path] = None) -> HudConfig:
    cfg_path = Path(path) if path else CONFIG_PATH
    data = _defaults()
    if cfg_path.is_file():
        try:
            file_data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(file_data, dict):
                data = _deep_merge(data, file_data)
        except (OSError, ValueError):
            # A malformed config must never stop a recording; fall back to
            # defaults and let the caller log it.
            data = _defaults()

    stt = data.get("stt", {})
    answers = data.get("answers", {})
    kb = data.get("kb", {})
    hud = data.get("hud", {})
    speakers = data.get("speakers", {})
    budget = data.get("budget", {})

    cfg = HudConfig(
        stt_backend=str(stt.get("backend") or "groq"),
        stt_model=stt.get("model"),
        stt_chunk_seconds=float(stt.get("chunk_seconds") or 14.0),
        stt_min_speech_seconds=float(stt.get("min_speech_seconds") or 0.6),
        stt_whisper_bin=str(stt.get("whisper_bin") or "whisper-server"),
        answers_enabled=bool(answers.get("enabled", True)),
        answers_backend=str(answers.get("backend") or "groq"),
        answers_fallback=[str(x) for x in (answers.get("fallback") or [])],
        chat_model=answers.get("chat_model"),
        rolling_model=answers.get("rolling_model"),
        answer_interval=float(answers.get("interval") or 35.0),
        rolling_enabled=bool(answers.get("rolling_enabled", True)),
        answer_max_tokens=int(answers.get("max_tokens") or 400),
        context_minutes=float(answers.get("context_minutes") or 3.0),
        question_cooldown=float(answers.get("question_cooldown") or 6.0),
        kb_enabled=bool(kb.get("enabled", True)),
        kb_dirs=[str(x) for x in (kb.get("dirs") or [])],
        kb_model=str(kb.get("model") or "all-MiniLM-L6-v2"),
        kb_top_k=int(kb.get("top_k") or 5),
        kb_reindex=bool(kb.get("reindex", False)),
        kb_cache_dir=kb.get("cache_dir"),
        speakers_enabled=bool(speakers.get("enabled", True)),
        self_name=str(speakers.get("self_name") or "You"),
        remote_name=str(speakers.get("remote_name") or "Others"),
        port=int(hud.get("port") or 0),
        open_browser=bool(hud.get("open_browser", True)),
        host=str(hud.get("host") or "127.0.0.1"),
        budget_tpm=int(budget.get("tpm") or 0),
        budget_tpd=int(budget.get("tpd") or 0),
        api_keys={str(k): str(v) for k, v in (data.get("api_keys") or {}).items()},
    )

    # Environment overrides for convenience.
    env_backend = os.environ.get("ZOOM_HUD_ANSWER_BACKEND")
    if env_backend:
        cfg.answers_backend = env_backend
    env_stt = os.environ.get("ZOOM_HUD_STT_BACKEND")
    if env_stt:
        cfg.stt_backend = env_stt
    env_dirs = os.environ.get("ZOOM_HUD_KB_DIRS")
    if env_dirs:
        cfg.kb_dirs = [p for p in env_dirs.split(os.pathsep) if p]
    env_port = os.environ.get("ZOOM_HUD_PORT")
    if env_port and env_port.isdigit():
        cfg.port = int(env_port)
    return cfg
