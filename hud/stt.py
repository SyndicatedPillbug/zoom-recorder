#!/usr/bin/env python3
"""Live speech-to-text for the HUD.

A dedicated, *isolated* ffmpeg process taps the same mic/system devices the
recorder uses and emits 16 kHz mono PCM. That stream is energy-gated, chopped
into chunks, and transcribed either by a remote OpenAI-compatible endpoint
(Groq/OpenAI) or locally via whisper.cpp.

Nothing here can affect the recording: it is a separate subprocess and separate
threads, and every failure is caught and reported to the HUD status line.
"""

from __future__ import annotations

import io
import math
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Iterator, List, Optional

from .config import HudConfig, LOCAL_STT_BACKENDS
from .llm import LLMClient, LLMError
from .state import LiveState

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # int16
CHANNELS = 1


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------
def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def frame_rms_dbfs(pcm: bytes) -> float:
    """Peak frame level in dBFS for a small PCM block (0 dB == full scale)."""
    if len(pcm) < SAMPLE_WIDTH:
        return -120.0
    count = len(pcm) // SAMPLE_WIDTH
    samples = struct.unpack("<{}h".format(count), pcm[: count * SAMPLE_WIDTH])
    if not samples:
        return -120.0
    peak = max(abs(s) for s in samples)
    if peak == 0:
        return -120.0
    return 20.0 * math.log10(peak / 32768.0)


def overlap_suffix_prefix(prev_words: List[str], new_words: List[str],
                          max_overlap: int = 12) -> int:
    """Length of the longest run where prev's tail equals new's head."""
    max_k = min(len(prev_words), len(new_words), max_overlap)
    for k in range(max_k, 0, -1):
        if [w.lower() for w in prev_words[-k:]] == [w.lower() for w in new_words[:k]]:
            return k
    return 0


def split_words(text: str) -> List[str]:
    return [w for w in (text or "").split() if w]


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------
class Chunker:
    """Energy-gated fixed-size chunker.

    Emits a chunk once it reaches ``chunk_seconds`` of speech, or earlier when a
    clear silence run follows speech (cuts latency). Pure-silence buffers are
    dropped so we never pay to transcribe dead air.
    """

    def __init__(self, chunk_seconds: float, min_speech_seconds: float = 0.6,
                 silence_flush_seconds: float = 1.4, silence_db: float = -50.0,
                 frame_ms: int = 100) -> None:
        self.chunk_bytes = int(chunk_seconds * SAMPLE_RATE * SAMPLE_WIDTH)
        self.min_speech_bytes = int(min_speech_seconds * SAMPLE_RATE * SAMPLE_WIDTH)
        self.silence_flush_bytes = int(silence_flush_seconds * SAMPLE_RATE * SAMPLE_WIDTH)
        self.silence_db = silence_db
        self.frame_bytes = int(frame_ms / 1000.0 * SAMPLE_RATE * SAMPLE_WIDTH)
        self._buf = bytearray()
        self._speech_bytes = 0
        self._silence_run = 0

    def feed(self, pcm: bytes) -> List[bytes]:
        out: List[bytes] = []
        self._buf += pcm
        # Classify whole 100 ms frames.
        while len(self._buf) >= self.frame_bytes:
            frame = bytes(self._buf[: self.frame_bytes])
            del self._buf[: self.frame_bytes]
            if frame_rms_dbfs(frame) > self.silence_db:
                self._speech_bytes += len(frame)
                self._silence_run = 0
                self._pending = getattr(self, "_pending", bytearray())
                self._pending += frame
            else:
                self._pending = getattr(self, "_pending", bytearray())
                if self._speech_bytes > 0:
                    self._pending += frame
                    self._silence_run += len(frame)
                else:
                    self._pending = bytearray()

            pending = self._pending
            if self._speech_bytes >= self.min_speech_bytes:
                if len(pending) >= self.chunk_bytes:
                    out.append(bytes(pending[: self.chunk_bytes]))
                    del pending[: self.chunk_bytes]
                    self._speech_bytes = max(0, self._speech_bytes - self.chunk_bytes)
                    self._silence_run = 0
                elif self._silence_run >= self.silence_flush_bytes:
                    out.append(bytes(pending))
                    pending.clear()
                    self._speech_bytes = 0
                    self._silence_run = 0
        return out

    def flush(self) -> Optional[bytes]:
        pending = getattr(self, "_pending", bytearray())
        if self._speech_bytes >= self.min_speech_bytes and len(pending) > SAMPLE_RATE:
            data = bytes(pending)
            pending.clear()
            self._speech_bytes = 0
            return data
        return None


# --------------------------------------------------------------------------
# STT backends
# --------------------------------------------------------------------------
class RemoteSTT:
    def __init__(self, client: LLMClient, model: str, log: Callable[[str], None]) -> None:
        self.client = client
        self.model = model
        self.log = log

    def transcribe(self, pcm: bytes) -> str:
        wav = pcm_to_wav(pcm)
        result = self.client.transcribe(wav, self.model, filename="chunk.wav")
        return result.text


class LocalWhisperSTT:
    """Local whisper.cpp transcription.

    Prefers a long-lived ``whisper-server`` (model stays hot) and falls back to
    invoking ``whisper-cli`` per chunk.
    """

    def __init__(self, model_path: Path, log: Callable[[str], None],
                 bin_name: str = "whisper-server") -> None:
        self.model = Path(model_path)
        self.log = log
        self.bin_name = bin_name
        self._server: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._cli = shutil.which("whisper-cli") or shutil.which("whisper-cpp")
        if shutil.which(bin_name):
            self._start_server()

    def _free_port(self) -> int:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _start_server(self) -> None:
        port = self._free_port()
        cmd = [self.bin_name, "-m", str(self.model), "--host", "127.0.0.1",
               "--port", str(port)]
        try:
            self._server = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.log("local STT: could not start {}: {}".format(self.bin_name, exc))
            self._server = None
            return
        self._port = port
        for _ in range(40):
            if self._server.poll() is not None:
                self.log("local STT: whisper-server exited early; falling back to whisper-cli")
                self._server = None
                self._port = None
                return
            try:
                import urllib.request
                urllib.request.urlopen(
                    "http://127.0.0.1:{}/".format(port), timeout=0.4)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.15)
        self.log("local STT: whisper-server on port {}".format(port))

    def transcribe(self, pcm: bytes) -> str:
        wav = pcm_to_wav(pcm)
        if self._server is not None and self._port is not None:
            try:
                return self._server_transcribe(wav)
            except Exception as exc:  # noqa: BLE001
                self.log("local STT: server request failed ({}); using whisper-cli".format(exc))
        return self._cli_transcribe(wav)

    def _server_transcribe(self, wav: bytes) -> str:
        from .llm import _encode_multipart  # reuse encoder
        body, content_type = _encode_multipart(
            {"response_format": "json", "temperature": "0.0"},
            [("file", "chunk.wav", "audio/wav", wav)],
        )
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:{}/inference".format(self._port),
            data=body, headers={"Content-Type": content_type}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            import json
            obj = json.loads(resp.read().decode("utf-8"))
        return (obj.get("text") or "").strip()

    def _cli_transcribe(self, wav: bytes) -> str:
        if not self._cli:
            raise RuntimeError("no local whisper binary available")
        with tempfile.TemporaryDirectory(prefix="zoomrec_stt_") as tmp:
            wav_path = Path(tmp) / "chunk.wav"
            out_base = Path(tmp) / "out"
            wav_path.write_bytes(wav)
            cmd = [self._cli, "-m", str(self.model), "-f", str(wav_path),
                   "-otxt", "-of", str(out_base), "-np"]
            subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            out_file = out_base.with_suffix(".txt")
            if out_file.is_file():
                return out_file.read_text(encoding="utf-8", errors="replace").strip()
        return ""

    def close(self) -> None:
        if self._server is not None:
            try:
                self._server.terminate()
                self._server.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self._server.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._server = None


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
class _Source:
    """One independent audio source (mic or system/loopback).

    Each source gets its own ffmpeg tap, chunker and transcription thread, so
    speech can be attributed to the channel it arrived on -- no diarization
    model needed for the two-party case.
    """

    def __init__(self, speaker: Optional[str], cmd: List[str]) -> None:
        self.speaker = speaker
        self.cmd = cmd
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.tail: List[str] = []


class LiveTranscriber:
    def __init__(self, state: LiveState, log: Callable[[str], None], cfg: HudConfig,
                 mic_name: Optional[str], system_name: Optional[str] = None,
                 model_path: Optional[Path] = None) -> None:
        self.state = state
        self.log = log
        self.cfg = cfg
        self.mic_name = mic_name
        self.system_name = system_name
        self.model_path = model_path
        self._stop = threading.Event()
        self._sources: List[_Source] = []
        self._stt: object = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        try:
            self._stt = self._build_stt()
        except Exception as exc:  # noqa: BLE001
            self.log("live STT disabled: {}".format(exc))
            self.state.set_status("recording", stt_error=str(exc))
            return
        try:
            self._sources = self._build_sources()
        except Exception as exc:  # noqa: BLE001
            self.log("live tap disabled: {}".format(exc))
            self.state.set_status("recording", tap_error=str(exc))
            return

        label = " + ".join(s.speaker or "mixed" for s in self._sources)
        self.log("live STT running ({} backend, {:.0f}s chunks, sources: {})".format(
            self.cfg.stt_backend, self.cfg.stt_chunk_seconds, label))
        for source in self._sources:
            source.thread = threading.Thread(
                target=self._run_source, args=(source,),
                name="hud-stt-{}".format(source.speaker or "mix"), daemon=True)
            source.thread.start()

    def stop(self) -> None:
        self._stop.set()
        for source in self._sources:
            proc = source.proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
        for source in self._sources:
            if source.thread is not None:
                source.thread.join(timeout=8.0)
        closer = getattr(self._stt, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    # -- setup -------------------------------------------------------------
    def _build_stt(self):
        if (self.cfg.stt_backend or "").lower() in LOCAL_STT_BACKENDS:
            if self.model_path is None:
                raise RuntimeError("local STT needs --model pointing at a ggml model")
            return LocalWhisperSTT(self.model_path, self.log, self.cfg.stt_whisper_bin)
        provider = self.cfg.provider_for_stt()
        if provider is None:
            raise RuntimeError("unknown STT backend '{}'".format(self.cfg.stt_backend))
        api_key = self.cfg.api_key_for(provider.name)
        if provider.api_key_env and not api_key:
            raise RuntimeError(
                "missing API key for STT provider '{}' (set {})".format(
                    provider.name, provider.api_key_env))
        client = LLMClient(provider.base_url, api_key)
        model = self.cfg.resolve_stt_model()
        if not model:
            raise RuntimeError("STT provider '{}' has no speech endpoint".format(provider.name))
        return RemoteSTT(client, model, self.log)

    # -- sources -----------------------------------------------------------
    def _device_cmd(self, name: str) -> List[str]:
        return ["ffmpeg", "-hide_banner", "-nostdin", "-thread_queue_size", "1024",
                "-f", "avfoundation", "-i", ":{}".format(name),
                "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]

    def _mixed_cmd(self, mic: str, system: str) -> List[str]:
        return ["ffmpeg", "-hide_banner", "-nostdin", "-thread_queue_size", "1024",
                "-f", "avfoundation", "-i", ":{}".format(mic),
                "-thread_queue_size", "1024",
                "-f", "avfoundation", "-i", ":{}".format(system),
                "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest[a]",
                "-map", "[a]", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]

    def _file_cmd(self, path: str) -> List[str]:
        return ["ffmpeg", "-hide_banner", "-nostdin", "-i", path,
                "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]

    def _build_sources(self) -> List[_Source]:
        if self.cfg.audio_file:
            return [_Source(None, self._file_cmd(self.cfg.audio_file))]
        if not self.mic_name:
            raise RuntimeError("no microphone device selected for the live tap")
        labelled = self.cfg.speakers_enabled
        if self.system_name and labelled:
            # Two independent channels -> accurate two-party attribution with
            # no diarization model: mic is you, loopback is everyone else.
            return [
                _Source(self.cfg.self_name, self._device_cmd(self.mic_name)),
                _Source(self.cfg.remote_name, self._device_cmd(self.system_name)),
            ]
        if self.system_name:
            return [_Source(None, self._mixed_cmd(self.mic_name, self.system_name))]
        return [_Source(self.cfg.self_name if labelled else None,
                        self._device_cmd(self.mic_name))]

    # -- per-source loop ---------------------------------------------------
    def _run_source(self, source: _Source) -> None:
        try:
            source.proc = subprocess.Popen(
                source.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.log("live tap failed to start ({}): {}".format(
                source.speaker or "mixed", exc))
            self.state.set_status("recording", tap_error=str(exc))
            return
        chunker = Chunker(self.cfg.stt_chunk_seconds, self.cfg.stt_min_speech_seconds)
        assert source.proc.stdout is not None
        try:
            for block in self._iter_blocks(source.proc.stdout):
                for chunk in chunker.feed(block):
                    self._transcribe_chunk(chunk, source)
            tail = chunker.flush()
            if tail:
                self._transcribe_chunk(tail, source)
        except Exception as exc:  # noqa: BLE001
            self.log("live transcription loop stopped ({}): {}".format(
                source.speaker or "mixed", exc))

    def _iter_blocks(self, stream, block_bytes: int = 6400) -> Iterator[bytes]:
        while not self._stop.is_set():
            data = stream.read(block_bytes)
            if not data:
                return
            yield data

    def _transcribe_chunk(self, chunk: bytes, source: "_Source") -> None:
        if self._stop.is_set():
            return
        started = time.time()
        try:
            text = self._stt.transcribe(chunk)  # type: ignore[attr-defined]
        except LLMError as exc:
            self.log("STT error: {}".format(exc))
            self.state.add("status", status="recording", stt_error=str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.log("STT failure: {}".format(exc))
            return
        if not text:
            return
        delta = self._delta_text(source, text)
        if delta:
            self.state.add("transcript", text=delta, source="live",
                           speaker=source.speaker,
                           latency=round(time.time() - started, 2))

    def _delta_text(self, source: "_Source", text: str) -> str:
        words = split_words(text)
        if not words:
            return ""
        k = overlap_suffix_prefix(source.tail, words)
        new_words = words[k:]
        source.tail = (source.tail + new_words)[-12:]
        return " ".join(new_words)
