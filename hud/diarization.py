#!/usr/bin/env python3
"""Optional post-call speaker attribution.

This module is intentionally an adapter, not part of the live path.  When
WhisperX and its diarization dependencies are installed and explicitly
enabled, it processes the saved remote track after recording.  If the tool,
model, token, or runtime is unavailable, the original channel-labelled
transcript remains the source of truth and the call still completes normally.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .voice_profiles import VoiceProfileStore, _vector


def _speaker_id(raw: str, ids: Dict[str, str]) -> str:
    key = str(raw or "UNKNOWN").strip() or "UNKNOWN"
    if key not in ids:
        ids[key] = "remote:{}".format(len(ids) + 1)
    return ids[key]


def _load_segments(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("segments", []) if isinstance(data, dict) else []
    speaker_embeddings = data.get("speaker_embeddings", {}) if isinstance(data, dict) else {}
    if not isinstance(speaker_embeddings, dict):
        speaker_embeddings = {}
    out: List[Dict[str, Any]] = []
    ids: Dict[str, str] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        raw_speaker = str(item.get("speaker") or "UNKNOWN")
        embedding = _vector(item.get("embedding"))
        if embedding is None:
            embedding = _vector(speaker_embeddings.get(raw_speaker))
        out.append({
            "start": start,
            "end": end,
            "speaker_id": _speaker_id(raw_speaker, ids),
            "speaker_label": "Remote {}".format(len(ids)),
            "speaker_source": "diarization",
            "speaker_confidence": float(item.get("speaker_confidence") or 0.5),
            "text": str(item.get("text") or "").strip(),
            "_embedding": embedding,
        })
    return out


def _resolve_hf_token() -> Optional[str]:
    """Resolve a local Hugging Face token without ever logging or exposing it."""
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if token:
        return token.strip() or None
    for candidate in (
        Path.home() / ".cache" / "huggingface" / "token",
        Path.home() / ".huggingface" / "token",
    ):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if value:
            return value
    return None


def _whisperx_binary(backend: str) -> Optional[str]:
    """Find WhisperX, preferring the repo-local optional environment."""
    if backend not in ("auto", "whisperx"):
        return None
    configured = os.environ.get("WHISPERX_BIN")
    if configured and Path(configured).is_file():
        return configured
    repo_local = Path(__file__).resolve().parent.parent / ".venv-diarization" / "bin" / "whisperx"
    if repo_local.is_file():
        return str(repo_local)
    return shutil.which("whisperx")


def _add_optional_embeddings(audio_path: Path, segments: List[Dict[str, Any]],
                             token: str, log: Callable[[str], None]) -> None:
    """Attach one representative embedding per diarized speaker when available."""
    try:
        from pyannote.audio import Inference, Model
        from pyannote.core import Segment
    except ImportError:
        return
    try:
        model = Model.from_pretrained("pyannote/embedding", token=token)
        inference = Inference(model, window="whole")
        representatives: Dict[str, Dict[str, Any]] = {}
        for segment in segments:
            current = representatives.get(segment["speaker_id"])
            if current is None or (segment["end"] - segment["start"]
                                   > current["end"] - current["start"]):
                representatives[segment["speaker_id"]] = segment
        embeddings: Dict[str, List[float]] = {}
        for speaker_id, segment in representatives.items():
            start = float(segment["start"])
            end = min(float(segment["end"]), start + 30.0)
            value = inference.crop(str(audio_path), Segment(start, end))
            if hasattr(value, "tolist"):
                value = value.tolist()
            normalized = _vector(value)
            if normalized is not None:
                embeddings[speaker_id] = normalized
        for segment in segments:
            if segment["speaker_id"] in embeddings:
                segment["_embedding"] = embeddings[segment["speaker_id"]]
        if embeddings:
            log("voice profiles: generated {} local speaker embedding(s)".format(
                len(embeddings)))
    except Exception as exc:  # noqa: BLE001
        # An optional model must never turn a successful diarization into a
        # failed recording or erase the generic speaker result.
        log("voice profiles: embedding enrichment unavailable ({})".format(exc))


def _render_transcript(events: Iterable[Dict[str, Any]], segments: List[Dict[str, Any]],
                       started_epoch: Optional[float], mappings: Dict[str, Dict[str, Any]]) -> str:
    lines = ["# Diarized transcript", "", "_Generated after recording; raw live events remain authoritative._", ""]
    for event in events:
        if event.get("type") != "transcript":
            continue
        text = str(event.get("text") or "").strip()
        if not text:
            continue
        captured = event.get("captured_at")
        relative = None
        if captured is not None and started_epoch is not None:
            try:
                relative = float(captured) - started_epoch
            except (TypeError, ValueError):
                relative = None
        match = None
        if relative is not None:
            match = next((seg for seg in segments
                          if seg["start"] <= relative <= seg["end"]), None)
        speaker_id = str(event.get("speaker_id") or "remote")
        label = str(event.get("speaker") or "Remote")
        confidence = None
        if match:
            speaker_id = match["speaker_id"]
            confidence = match["speaker_confidence"]
            label = match["speaker_label"]
        override = mappings.get(speaker_id, {}).get("label")
        if override:
            label = str(override)
        stamp = event.get("ts")
        try:
            import time
            clock = time.strftime("%H:%M:%S", time.localtime(float(stamp)))
        except (TypeError, ValueError, OverflowError):
            clock = "--:--:--"
        suffix = ""
        if confidence is not None:
            suffix = " _[{} confidence {:.0%}]_".format(speaker_id, confidence)
        lines.append("[{}] **{}:** {}{}".format(clock, label, text, suffix))
    return "\n".join(lines).rstrip() + "\n"


def run_post_call_diarization(audio_path: Path, events: Iterable[Dict[str, Any]],
                              output_dir: Path, cfg: Any, log: Callable[[str], None],
                              started_epoch: Optional[float] = None,
                              mappings: Optional[Dict[str, Dict[str, Any]]] = None
                              ) -> Optional[Dict[str, Any]]:
    """Run a bounded, optional WhisperX attribution pass.

    The command is assembled from fixed arguments and never through a shell.
    No API key is logged.  Output is written only below ``derived/``.
    """
    if not getattr(cfg, "diarization_enabled", False):
        return None
    backend = str(getattr(cfg, "diarization_backend", "auto") or "auto").lower()
    if backend in ("off", "none", "disabled"):
        return None
    if not audio_path.is_file():
        log("diarization skipped: remote audio track is unavailable")
        return None
    binary = _whisperx_binary(backend)
    if not binary:
        log("diarization skipped: install WhisperX to enable the optional post-call pass")
        return None
    token = _resolve_hf_token()
    if not token:
        log("diarization skipped: HF_TOKEN is not set for the diarization model")
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="diarization-", dir=str(output_dir)) as tmp:
        command = [binary, str(audio_path), "--model", "large-v3-turbo",
                   "--output_dir", tmp, "--output_format", "json", "--diarize",
                   "--device", "cpu", "--compute_type", "int8"]
        if getattr(cfg, "voice_profiles_enabled", True):
            command.append("--speaker_embeddings")
        # Keep the credential out of the process argument list.  Hugging Face
        # libraries and WhisperX both honor HF_TOKEN in the child environment.
        child_env = os.environ.copy()
        child_env["HF_TOKEN"] = token
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True,
                cwd=str(Path(__file__).resolve().parent.parent),
                env=child_env,
                timeout=max(30.0, float(getattr(cfg, "diarization_timeout_seconds", 300.0))))
        except (OSError, subprocess.SubprocessError) as exc:
            log("diarization skipped after launch failure: {}".format(exc))
            return None
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "unknown failure").strip().splitlines()
            log("diarization skipped: {}".format(detail[-1] if detail else "unknown failure"))
            return None
        candidates = sorted(Path(tmp).glob("*.json"))
        if not candidates:
            log("diarization skipped: WhisperX produced no JSON output")
            return None
        try:
            segments = _load_segments(candidates[0])
        except (OSError, ValueError, TypeError) as exc:
            log("diarization skipped: invalid WhisperX output ({})".format(exc))
            return None

    if not segments:
        log("diarization completed with no speaker segments")
        return None
    if (getattr(cfg, "voice_profiles_enabled", True)
            and not any(segment.get("_embedding") for segment in segments)):
        _add_optional_embeddings(audio_path, segments, token, log)
    events_list = list(events)
    mapping = mappings or {}
    store = None
    if (getattr(cfg, "voice_profiles_enabled", True)
            and getattr(cfg, "voice_profiles_path", None) is not False):
        store = VoiceProfileStore(
            Path(getattr(cfg, "voice_profiles_path", None)).expanduser()
            if getattr(cfg, "voice_profiles_path", None) else None,
            threshold=float(getattr(cfg, "voice_profile_threshold", 0.78)),
            log=log)
    diar_speaker_count = len({seg["speaker_id"] for seg in segments})
    enrolled: List[Dict[str, Any]] = []
    enrolled_speakers = set()
    matched = 0
    single_remote_label = None
    if diar_speaker_count == 1:
        remote_mapping = mapping.get("remote")
        if remote_mapping and remote_mapping.get("source") == "user":
            single_remote_label = str(remote_mapping.get("label") or "").strip() or None
    for segment in segments:
        embedding = segment.pop("_embedding", None)
        if store is None or embedding is None:
            continue
        manual = mapping.get(segment["speaker_id"])
        manual_label = None
        if manual and manual.get("source") == "user":
            manual_label = str(manual.get("label") or "").strip() or None
        manual_label = manual_label or single_remote_label
        if manual_label:
            if segment["speaker_id"] in enrolled_speakers:
                continue
            profile = store.enroll(manual_label, embedding,
                                   Path(audio_path).parent.name)
            if profile:
                segment.update({
                    "speaker_label": manual_label,
                    "speaker_source": "user",
                    "speaker_confidence": 1.0,
                    "profile_id": profile["profile_id"],
                })
                enrolled.append(profile)
                enrolled_speakers.add(segment["speaker_id"])
            continue
        profile = store.match(embedding)
        if profile and profile["confidence"] >= 0.65:
            segment.update({
                "speaker_label": profile["label"],
                "speaker_source": "voice_profile",
                "speaker_confidence": profile["confidence"],
                "profile_id": profile["profile_id"],
                "profile_similarity": profile["similarity"],
            })
            matched += 1

    public_segments = [{key: value for key, value in segment.items()
                        if not key.startswith("_")}
                       for segment in segments]
    result = {
        "schema_version": 1,
        "backend": "whisperx",
        "audio": str(audio_path),
        "segments": public_segments,
        "speaker_count": diar_speaker_count,
        "voice_profile_matches": matched,
        "voice_profiles_enrolled": enrolled,
    }
    (output_dir / "diarization.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output_dir / "diarized_transcript.md").write_text(
        _render_transcript(events_list, public_segments, started_epoch, mapping),
        encoding="utf-8")
    log("diarization complete: {} speaker(s) in derived/diarization.json".format(
        result["speaker_count"]))
    return result
