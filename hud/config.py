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
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_PATH = Path.home() / ".config" / "zoom-recorder" / "config.json"

DEFAULT_TRANSCRIPTION_MODEL = "~/.cache/whisper-cpp/ggml-base.en.bin"
RECORDING_MODES = ("both", "mic", "system")


@dataclass
class RecorderDefaults:
    """Recorder-side settings the GUI can control (zoom_record.py reads
    these as defaults; CLI flags still win)."""

    basedir: str = "~/ZoomRecordings"
    mic: Optional[str] = None
    mode: str = "both"                       # both | mic | system
    notifications: bool = True
    offline: bool = False
    transcription_model: str = DEFAULT_TRANSCRIPTION_MODEL

    def record_mic(self) -> bool:
        return self.mode in ("both", "mic")

    def record_system(self) -> bool:
        return self.mode in ("both", "system")


def recorder_defaults(data: Optional[Dict[str, Any]] = None) -> RecorderDefaults:
    """Build RecorderDefaults from a config dict (already merged with
    defaults) or from the config file on disk."""
    if data is None:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    rec = (data or {}).get("recorder") or {}
    mode = str(rec.get("mode") or "both").strip().lower()
    if mode not in RECORDING_MODES:
        mode = "both"
    return RecorderDefaults(
        basedir=str(rec.get("basedir") or "~/ZoomRecordings"),
        mic=(rec.get("mic") or None),
        mode=mode,
        notifications=bool(rec.get("notifications", True)),
        offline=bool(rec.get("offline", False)),
        transcription_model=str(rec.get("transcription_model")
                                or DEFAULT_TRANSCRIPTION_MODEL),
    )


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
    stt_chunk_seconds: float = 10.0    # Groq bills a 10s minimum per request
    stt_min_speech_seconds: float = 0.8
    stt_whisper_bin: str = "whisper-server"
    stt_glossary: List[str] = field(default_factory=list)
    stt_queue_chunks: int = 4          # bounded per-source STT queue (drop-oldest)
    # Voice-activity detection / anti-hallucination
    stt_vad_backend: str = "auto"      # auto | energy | webrtcvad
    stt_adaptive_vad: bool = True
    stt_silence_db: float = -50.0
    stt_vad_margin_db: float = 6.0
    stt_vad_aggressiveness: int = 1    # webrtcvad 0 (lenient) .. 3 (strict)
    stt_verbose_stt: bool = True       # request segment confidences (verbose_json)
    stt_no_speech_prob_max: float = 0.75
    stt_avg_logprob_min: float = -1.5
    stt_compression_ratio_max: float = 2.4
    stt_hallucination_filter: bool = True
    stt_context_prompt: bool = True    # seed Whisper with previous transcript

    # Answers
    answers_enabled: bool = True
    answers_backend: str = "groq"
    answers_fallback: List[str] = field(default_factory=list)
    chat_model: Optional[str] = None
    rolling_model: Optional[str] = None
    answer_interval: float = 35.0
    rolling_enabled: bool = True
    answer_max_tokens: int = 600
    context_minutes: float = 5.0
    context_max_chars: int = 6000
    question_cooldown: float = 6.0
    question_lookback_seconds: float = 90.0
    question_rewrite: bool = True
    answer_self_questions: bool = False
    point_dedupe_score: float = 0.9
    max_context_qa: int = 3
    summary_enabled: bool = True
    summary_model: Optional[str] = None
    # Talking-point grounding: strictly transcript-anchored, quote-verified,
    # and gated on enough new speech so sparse audio yields nothing.
    talking_points_grounded: bool = True
    talking_points_max: int = 3
    talking_points_min_new_words: int = 60
    talking_points_min_words: int = 40
    talking_points_quote_overlap: float = 0.7

    # Knowledge base
    kb_enabled: bool = True
    kb_dirs: List[str] = field(default_factory=list)
    kb_model: str = "all-MiniLM-L6-v2"
    kb_top_k: int = 5
    kb_reindex: bool = False
    kb_cache_dir: Optional[str] = None
    kb_embed_backend: str = "auto"      # auto | sentence-transformers | ollama | openai
    kb_embed_model: Optional[str] = None
    kb_min_score: float = 0.1

    # Speaker labelling (channel-based: mic vs system/loopback)
    speakers_enabled: bool = True
    self_name: str = "You"
    remote_name: str = "Others"

    # HUD server
    port: int = 0
    open_browser: bool = True
    host: str = "127.0.0.1"
    persist_seconds: float = 20.0     # periodic crash-safe flush of derived/

    # Budget caps (0 == trust the provider's rate-limit headers)
    budget_tpm: int = 0
    budget_tpd: int = 0

    # Privacy: offline hard-disables all non-loopback network access;
    # notifications turns the recorder's desktop notifications on/off.
    offline: bool = False
    notifications: bool = True

    # Recorder-side settings (basedir, mic, recording mode, model).
    recorder: RecorderDefaults = field(default_factory=RecorderDefaults)

    # Set once the setup wizard has been completed.
    onboarded: bool = False

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
            "chunk_seconds": 10.0,
            "min_speech_seconds": 0.8,
            "whisper_bin": "whisper-server",
            "glossary": [],
            "queue_chunks": 4,
            "vad_backend": "auto",
            "adaptive_vad": True,
            "silence_db": -50.0,
            "vad_margin_db": 6.0,
            "vad_aggressiveness": 1,
            "verbose_stt": True,
            "no_speech_prob_max": 0.75,
            "avg_logprob_min": -1.5,
            "compression_ratio_max": 2.4,
            "hallucination_filter": True,
            "context_prompt": True,
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
            "context_minutes": 5.0,
            "context_max_chars": 6000,
            "question_cooldown": 6.0,
            "question_lookback_seconds": 90.0,
            "question_rewrite": True,
            "answer_self_questions": False,
            "point_dedupe_score": 0.9,
            "max_context_qa": 3,
            "summary_enabled": True,
            "summary_model": None,
            "talking_points_grounded": True,
            "talking_points_max": 3,
            "talking_points_min_new_words": 60,
            "talking_points_min_words": 40,
            "talking_points_quote_overlap": 0.7,
        },
        "kb": {
            "enabled": True,
            "dirs": [],
            "model": "all-MiniLM-L6-v2",
            "top_k": 5,
            "reindex": False,
            "cache_dir": None,
            "embed_backend": "auto",
            "embed_model": None,
            "min_score": 0.1,
        },
        "hud": {"port": 0, "open_browser": True, "host": "127.0.0.1", "persist_seconds": 20.0},
        "speakers": {"enabled": True, "self_name": "You", "remote_name": "Others"},
        "budget": {"tpm": 0, "tpd": 0},
        "privacy": {"offline": False, "notifications": True},
        "recorder": {
            "basedir": "~/ZoomRecordings",
            "mic": None,
            "mode": "both",
            "notifications": True,
            "offline": False,
            "transcription_model": DEFAULT_TRANSCRIPTION_MODEL,
        },
        "onboarded": False,
        "api_keys": {},
    }


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [p.strip() for p in value.replace(os.pathsep, ",").split(",") if p.strip()]
    return []


def config_from_dict(data: Dict[str, Any]) -> HudConfig:
    """Build a HudConfig from a (already default-merged) nested dict."""
    stt = data.get("stt") or {}
    answers = data.get("answers") or {}
    kb = data.get("kb") or {}
    hud = data.get("hud") or {}
    speakers = data.get("speakers") or {}
    budget = data.get("budget") or {}
    privacy = data.get("privacy") or {}

    return HudConfig(
        stt_backend=str(stt.get("backend") or "groq"),
        stt_model=stt.get("model") or None,
        stt_chunk_seconds=_as_float(stt.get("chunk_seconds"), 10.0),
        stt_min_speech_seconds=_as_float(stt.get("min_speech_seconds"), 0.8),
        stt_whisper_bin=str(stt.get("whisper_bin") or "whisper-server"),
        stt_glossary=_as_str_list(stt.get("glossary")),
        stt_queue_chunks=_as_int(stt.get("queue_chunks"), 4),
        stt_vad_backend=str(stt.get("vad_backend") or "auto"),
        stt_adaptive_vad=bool(stt.get("adaptive_vad", True)),
        stt_silence_db=_as_float(stt.get("silence_db"), -50.0),
        stt_vad_margin_db=_as_float(stt.get("vad_margin_db"), 6.0),
        stt_vad_aggressiveness=_as_int(stt.get("vad_aggressiveness"), 1),
        stt_verbose_stt=bool(stt.get("verbose_stt", True)),
        stt_no_speech_prob_max=_as_float(stt.get("no_speech_prob_max"), 0.75),
        stt_avg_logprob_min=_as_float(stt.get("avg_logprob_min"), -1.5),
        stt_compression_ratio_max=_as_float(stt.get("compression_ratio_max"), 2.4),
        stt_hallucination_filter=bool(stt.get("hallucination_filter", True)),
        stt_context_prompt=bool(stt.get("context_prompt", True)),
        answers_enabled=bool(answers.get("enabled", True)),
        answers_backend=str(answers.get("backend") or "groq"),
        answers_fallback=_as_str_list(answers.get("fallback")),
        chat_model=answers.get("chat_model") or None,
        rolling_model=answers.get("rolling_model") or None,
        answer_interval=_as_float(answers.get("interval"), 35.0),
        rolling_enabled=bool(answers.get("rolling_enabled", True)),
        answer_max_tokens=_as_int(answers.get("max_tokens"), 600),
        context_minutes=_as_float(answers.get("context_minutes"), 5.0),
        context_max_chars=_as_int(answers.get("context_max_chars"), 6000),
        question_cooldown=_as_float(answers.get("question_cooldown"), 6.0),
        question_lookback_seconds=_as_float(answers.get("question_lookback_seconds"), 90.0),
        question_rewrite=bool(answers.get("question_rewrite", True)),
        answer_self_questions=bool(answers.get("answer_self_questions", False)),
        point_dedupe_score=_as_float(answers.get("point_dedupe_score"), 0.9),
        max_context_qa=_as_int(answers.get("max_context_qa"), 3),
        summary_enabled=bool(answers.get("summary_enabled", True)),
        summary_model=answers.get("summary_model") or None,
        talking_points_grounded=bool(answers.get("talking_points_grounded", True)),
        talking_points_max=_as_int(answers.get("talking_points_max"), 3),
        talking_points_min_new_words=_as_int(answers.get("talking_points_min_new_words"), 60),
        talking_points_min_words=_as_int(answers.get("talking_points_min_words"), 40),
        talking_points_quote_overlap=_as_float(answers.get("talking_points_quote_overlap"), 0.7),
        kb_enabled=bool(kb.get("enabled", True)),
        kb_dirs=_as_str_list(kb.get("dirs")),
        kb_model=str(kb.get("model") or "all-MiniLM-L6-v2"),
        kb_top_k=_as_int(kb.get("top_k"), 5),
        kb_reindex=bool(kb.get("reindex", False)),
        kb_cache_dir=kb.get("cache_dir") or None,
        kb_embed_backend=str(kb.get("embed_backend") or "auto"),
        kb_embed_model=kb.get("embed_model") or None,
        kb_min_score=_as_float(kb.get("min_score"), 0.1),
        speakers_enabled=bool(speakers.get("enabled", True)),
        self_name=str(speakers.get("self_name") or "You"),
        remote_name=str(speakers.get("remote_name") or "Others"),
        port=_as_int(hud.get("port"), 0),
        open_browser=bool(hud.get("open_browser", True)),
        host=str(hud.get("host") or "127.0.0.1"),
        persist_seconds=_as_float(hud.get("persist_seconds"), 20.0),
        budget_tpm=_as_int(budget.get("tpm"), 0),
        budget_tpd=_as_int(budget.get("tpd"), 0),
        offline=bool(privacy.get("offline", False)),
        notifications=bool(privacy.get("notifications", True)),
        recorder=recorder_defaults(data),
        onboarded=bool(data.get("onboarded", False)),
        api_keys={str(k): str(v) for k, v in (data.get("api_keys") or {}).items()},
    )


def config_to_dict(cfg: HudConfig, include_keys: bool = True) -> Dict[str, Any]:
    """Inverse of :func:`config_from_dict`; used by the settings GUI."""
    out = _defaults()
    out["stt"].update({
        "backend": cfg.stt_backend,
        "model": cfg.stt_model,
        "chunk_seconds": cfg.stt_chunk_seconds,
        "min_speech_seconds": cfg.stt_min_speech_seconds,
        "whisper_bin": cfg.stt_whisper_bin,
        "glossary": list(cfg.stt_glossary),
        "queue_chunks": cfg.stt_queue_chunks,
        "vad_backend": cfg.stt_vad_backend,
        "adaptive_vad": cfg.stt_adaptive_vad,
        "silence_db": cfg.stt_silence_db,
        "vad_margin_db": cfg.stt_vad_margin_db,
        "vad_aggressiveness": cfg.stt_vad_aggressiveness,
        "verbose_stt": cfg.stt_verbose_stt,
        "no_speech_prob_max": cfg.stt_no_speech_prob_max,
        "avg_logprob_min": cfg.stt_avg_logprob_min,
        "compression_ratio_max": cfg.stt_compression_ratio_max,
        "hallucination_filter": cfg.stt_hallucination_filter,
        "context_prompt": cfg.stt_context_prompt,
    })
    out["answers"].update({
        "enabled": cfg.answers_enabled,
        "backend": cfg.answers_backend,
        "fallback": list(cfg.answers_fallback),
        "chat_model": cfg.chat_model,
        "rolling_model": cfg.rolling_model,
        "interval": cfg.answer_interval,
        "rolling_enabled": cfg.rolling_enabled,
        "max_tokens": cfg.answer_max_tokens,
        "context_minutes": cfg.context_minutes,
        "context_max_chars": cfg.context_max_chars,
        "question_cooldown": cfg.question_cooldown,
        "question_lookback_seconds": cfg.question_lookback_seconds,
        "question_rewrite": cfg.question_rewrite,
        "answer_self_questions": cfg.answer_self_questions,
        "point_dedupe_score": cfg.point_dedupe_score,
        "max_context_qa": cfg.max_context_qa,
        "summary_enabled": cfg.summary_enabled,
        "summary_model": cfg.summary_model,
        "talking_points_grounded": cfg.talking_points_grounded,
        "talking_points_max": cfg.talking_points_max,
        "talking_points_min_new_words": cfg.talking_points_min_new_words,
        "talking_points_min_words": cfg.talking_points_min_words,
        "talking_points_quote_overlap": cfg.talking_points_quote_overlap,
    })
    out["kb"].update({
        "enabled": cfg.kb_enabled,
        "dirs": list(cfg.kb_dirs),
        "model": cfg.kb_model,
        "top_k": cfg.kb_top_k,
        "reindex": cfg.kb_reindex,
        "cache_dir": cfg.kb_cache_dir,
        "embed_backend": cfg.kb_embed_backend,
        "embed_model": cfg.kb_embed_model,
        "min_score": cfg.kb_min_score,
    })
    out["speakers"].update({
        "enabled": cfg.speakers_enabled,
        "self_name": cfg.self_name,
        "remote_name": cfg.remote_name,
    })
    out["hud"].update({
        "port": cfg.port,
        "open_browser": cfg.open_browser,
        "host": cfg.host,
        "persist_seconds": cfg.persist_seconds,
    })
    out["budget"].update({"tpm": cfg.budget_tpm, "tpd": cfg.budget_tpd})
    out["privacy"].update({"offline": cfg.offline, "notifications": cfg.notifications})
    out["recorder"] = {
        "basedir": cfg.recorder.basedir,
        "mic": cfg.recorder.mic,
        "mode": cfg.recorder.mode,
        "notifications": cfg.recorder.notifications,
        "offline": cfg.recorder.offline,
        "transcription_model": cfg.recorder.transcription_model,
    }
    out["api_keys"] = dict(cfg.api_keys) if include_keys else {}
    out["onboarded"] = cfg.onboarded
    return out


def save_config(cfg: HudConfig, path: Optional[Path] = None,
                api_keys: Optional[Dict[str, str]] = None) -> Path:
    """Write the config safely.

    * values are deep-merged onto the existing file so unknown keys survive;
    * the previous file is backed up as ``config.json.bak``;
    * the write is atomic (temp file + ``os.replace``) and ``chmod 600``.

    Pass ``api_keys`` to control the stored keys explicitly (the GUI uses this
    so a blank form field never clears an existing key).
    """
    cfg_path = Path(path) if path else CONFIG_PATH
    existing: Dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}

    managed = config_to_dict(cfg, include_keys=False)
    managed["api_keys"] = ({str(k): str(v) for k, v in api_keys.items()}
                           if api_keys is not None else dict(cfg.api_keys))
    merged = _deep_merge(existing, managed)

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.is_file():
        try:
            shutil.copy2(str(cfg_path), str(cfg_path.parent / (cfg_path.name + ".bak")))
        except OSError:
            pass
    tmp = cfg_path.parent / (cfg_path.name + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(cfg_path))
    return cfg_path


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

    cfg = config_from_dict(data)

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
