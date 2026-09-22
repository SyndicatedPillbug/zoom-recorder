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

import difflib
import io
import math
import queue
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional

from .config import HudConfig, LOCAL_STT_BACKENDS
from .llm import LLMClient, LLMError
from .state import LiveState
from .vad import build_vad

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # int16
CHANNELS = 1

# Whisper is known to emit these on silence/noise. Bare interjections are only
# treated as hallucination when the chunk was marginal (see looks_hallucinated).
HALLUCINATION_PHRASES = re.compile(
    r"^\s*(?:thanks? for watching.*|thank you[.!]?|thanks[.!]?|"
    r"please (?:like|subscribe).*|"
    r"subtitles? by.*|amara\.org.*|transcription by.*|"
    r"♪+.*|\.+)\s*$",
    re.I,
)
BARE_INTERJECTIONS = {"you", "thank you", "thanks", "bye", "okay", "ok", "yeah", "hmm", "uh"}


@dataclass
class STTResult:
    """Transcription text plus optional segment-confidence signals."""

    text: str
    avg_logprob: Optional[float] = None
    no_speech_prob: Optional[float] = None
    compression_ratio: Optional[float] = None


def looks_hallucinated(text: str, marginal: bool = False,
                       avg_logprob: Optional[float] = None,
                       no_speech_prob: Optional[float] = None,
                       compression_ratio: Optional[float] = None,
                       no_speech_prob_max: float = 0.75,
                       avg_logprob_min: float = -1.5,
                       compression_ratio_max: float = 2.4) -> bool:
    """Heuristic filter for Whisper's non-speech output.

    Catches known silence phrases, repetitive loops, and low-confidence
    segments -- especially on chunks whose energy was only marginally above the
    noise floor.
    """
    t = (text or "").strip()
    if not t:
        return True
    if HALLUCINATION_PHRASES.match(t):
        return True
    if no_speech_prob is not None and no_speech_prob > no_speech_prob_max:
        return True
    if compression_ratio is not None and compression_ratio > compression_ratio_max:
        return True

    words = re.findall(r"[a-z0-9']+", t.lower())
    if len(words) >= 6:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio < 0.45:
            return True
    if len(words) >= 9:
        grams = [tuple(words[i:i + 3]) for i in range(len(words) - 2)]
        if grams:
            top = Counter(grams).most_common(1)[0][1]
            # A 3-gram repeated three or more times in one short chunk is a
            # classic Whisper loop, not natural speech.
            if top >= 3:
                return True

    if marginal:
        if avg_logprob is not None and avg_logprob < avg_logprob_min:
            return True
        if len(words) <= 2 and t.lower().strip(".,!? ") in BARE_INTERJECTIONS:
            return True
    return False



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


def _normalize_word(word: str) -> str:
    return word.lower().strip(".,!?;:\"'()[]")


def fuzzy_overlap(prev_words: List[str], new_words: List[str],
                  max_overlap: int = 12, threshold: float = 0.75) -> int:
    """Longest run where prev's tail matches new's head, tolerating ASR noise.

    Exact matches are preferred; near-matches (e.g. ``"roll out"`` vs
    ``"rollout"``, or one word differing in a three-word run) count when the
    character-level similarity is high enough. Returns the number of overlapped
    words.
    """
    max_k = min(len(prev_words), len(new_words), max_overlap)
    # Prefer an exact tail/head match so fuzzy matching never over-reaches.
    for k in range(max_k, 0, -1):
        a = " ".join(_normalize_word(w) for w in prev_words[-k:])
        b = " ".join(_normalize_word(w) for w in new_words[:k])
        if a == b:
            return k
    # No exact match: allow a high-similarity near-match (ASR variants).
    for k in range(max_k, 2, -1):
        a = " ".join(_normalize_word(w) for w in prev_words[-k:])
        b = " ".join(_normalize_word(w) for w in new_words[:k])
        if difflib.SequenceMatcher(None, a, b).ratio() >= threshold:
            return k
    return 0


class StablePartialDecoder:
    """Turn overlapping Whisper hypotheses into stable word commits.

    Whisper re-decodes a moving audio window, so the newest words are allowed
    to change. A prefix seen in two consecutive hypotheses is safe enough to
    publish; the remainder stays provisional. The caller still applies the
    normal transcript delta de-duplication before emitting an authoritative
    ``transcript`` event.
    """

    def __init__(self) -> None:
        self.previous: List[str] = []
        self.stable: List[str] = []
        self.current: List[str] = []

    @staticmethod
    def _same(a: str, b: str) -> bool:
        return _normalize_word(a) == _normalize_word(b)

    def _common_prefix(self, a: List[str], b: List[str]) -> int:
        count = 0
        for left, right in zip(a, b):
            if not self._same(left, right):
                break
            count += 1
        return count

    def accept(self, text: str) -> str:
        words = split_words(text)
        if not words:
            return ""
        # When a moving window drops old audio, retain only a matching tail of
        # the stable prefix. This prevents the decoder from blocking forever
        # while keeping the normal source-level de-duplication as a guard.
        if self.stable and (len(words) < len(self.stable) or not all(
                self._same(a, b) for a, b in zip(self.stable, words[:len(self.stable)]))):
            overlap = fuzzy_overlap(self.stable, words)
            self.stable = self.stable[-overlap:] if overlap else []

        common = self._common_prefix(self.previous, words) if self.previous else 0
        committed_len = len(self.stable)
        emit = words[committed_len:common] if common > committed_len else []
        if common > committed_len:
            self.stable = words[:common]
        self.previous = words
        self.current = words
        return " ".join(emit).strip()

    @property
    def provisional_text(self) -> str:
        stable_len = min(len(self.stable), len(self.current))
        return " ".join(self.current[stable_len:]).strip()


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------
class Chunker:
    """Energy-gated fixed-size chunker.

    Emits a chunk once it reaches ``chunk_seconds`` of speech, or earlier when a
    clear silence run follows speech (cuts latency). Pure-silence buffers are
    dropped so we never pay to transcribe dead air.
    """

    def __init__(self, chunk_seconds: float, min_speech_seconds: float = 0.8,
                 silence_flush_seconds: float = 1.4, silence_db: float = -50.0,
                 frame_ms: int = 100, phrase_silence_seconds: float = 0.6,
                 phrase_min_speech_seconds: float = 6.0, vad=None) -> None:
        self.chunk_bytes = int(chunk_seconds * SAMPLE_RATE * SAMPLE_WIDTH)
        self.min_speech_bytes = int(min_speech_seconds * SAMPLE_RATE * SAMPLE_WIDTH)
        self.silence_flush_bytes = int(silence_flush_seconds * SAMPLE_RATE * SAMPLE_WIDTH)
        self.phrase_silence_bytes = int(phrase_silence_seconds * SAMPLE_RATE * SAMPLE_WIDTH)
        self.phrase_min_bytes = int(phrase_min_speech_seconds * SAMPLE_RATE * SAMPLE_WIDTH)
        self.silence_db = silence_db
        self.vad = vad
        self.frame_bytes = int(frame_ms / 1000.0 * SAMPLE_RATE * SAMPLE_WIDTH)
        self.last_margin_db = 0.0
        self._buf = bytearray()
        self._speech_bytes = 0
        self._silence_run = 0

    def _is_speech(self, frame: bytes) -> bool:
        if self.vad is not None:
            speech = bool(self.vad.is_speech(frame))
            self.last_margin_db = float(getattr(self.vad, "last_margin_db", 0.0))
            return speech
        level = frame_rms_dbfs(frame)
        self.last_margin_db = level - self.silence_db
        return level > self.silence_db

    def feed(self, pcm: bytes) -> List[bytes]:
        out: List[bytes] = []
        self._buf += pcm
        # Classify whole 100 ms frames.
        while len(self._buf) >= self.frame_bytes:
            frame = bytes(self._buf[: self.frame_bytes])
            del self._buf[: self.frame_bytes]
            if self._is_speech(frame):
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
                elif (self.phrase_min_bytes and len(pending) >= self.phrase_min_bytes
                      and self._silence_run >= self.phrase_silence_bytes):
                    # A natural phrase pause after enough speech: emit now for
                    # lower latency without adding a billed STT request (the
                    # provider bills a 10 s minimum anyway).
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
    def __init__(self, client: LLMClient, model: str, log: Callable[[str], None],
                 verbose: bool = True, no_speech_prob_max: float = 0.75,
                 avg_logprob_min: float = -1.5,
                 compression_ratio_max: float = 2.4) -> None:
        self.client = client
        self.model = model
        self.log = log
        self.verbose = verbose
        self.no_speech_prob_max = no_speech_prob_max
        self.avg_logprob_min = avg_logprob_min
        self.compression_ratio_max = compression_ratio_max
        self._verbose_supported = verbose

    def transcribe(self, pcm: bytes, prompt: Optional[str] = None) -> STTResult:
        wav = pcm_to_wav(pcm)
        if self._verbose_supported:
            try:
                result = self.client.transcribe(
                    wav, self.model, filename="chunk.wav", prompt=prompt,
                    response_format="verbose_json")
                return self._from_verbose(result)
            except LLMError as exc:
                if exc.status != 400:
                    raise
                # Some OpenAI-compatible providers reject verbose_json.
                self._verbose_supported = False
                self.log("STT: verbose_json unsupported; falling back to json")
        result = self.client.transcribe(wav, self.model, filename="chunk.wav", prompt=prompt)
        return STTResult(text=result.text)

    def _segment_bad(self, seg: dict) -> bool:
        def num(key):
            try:
                return float(seg.get(key))
            except (TypeError, ValueError):
                return None

        no_speech = num("no_speech_prob")
        if no_speech is not None and no_speech > self.no_speech_prob_max:
            return True
        logprob = num("avg_logprob")
        if logprob is not None and logprob < self.avg_logprob_min:
            return True
        ratio = num("compression_ratio")
        if ratio is not None and ratio > self.compression_ratio_max:
            return True
        return False

    def _from_verbose(self, result) -> STTResult:
        segments = (result.data or {}).get("segments") or []
        if not segments:
            return STTResult(text=result.text)
        kept: List[str] = []
        logprobs: List[float] = []
        no_speech: List[float] = []
        ratios: List[float] = []
        for seg in segments:
            if not isinstance(seg, dict) or self._segment_bad(seg):
                continue
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            kept.append(text)
            for value, bucket in ((seg.get("avg_logprob"), logprobs),
                                  (seg.get("no_speech_prob"), no_speech),
                                  (seg.get("compression_ratio"), ratios)):
                try:
                    bucket.append(float(value))
                except (TypeError, ValueError):
                    pass
        return STTResult(
            text=" ".join(kept).strip() or result.text,
            avg_logprob=(sum(logprobs) / len(logprobs)) if logprobs else None,
            no_speech_prob=max(no_speech) if no_speech else None,
            compression_ratio=max(ratios) if ratios else None,
        )


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

    def transcribe(self, pcm: bytes, prompt: Optional[str] = None) -> STTResult:
        wav = pcm_to_wav(pcm)
        if self._server is not None and self._port is not None:
            try:
                return STTResult(text=self._server_transcribe(wav, prompt))
            except Exception as exc:  # noqa: BLE001
                self.log("local STT: server request failed ({}); using whisper-cli".format(exc))
        return STTResult(text=self._cli_transcribe(wav, prompt))

    def _server_transcribe(self, wav: bytes, prompt: Optional[str] = None) -> str:
        from .llm import _encode_multipart  # reuse encoder
        fields = {"response_format": "json", "temperature": "0.0"}
        if prompt:
            fields["prompt"] = prompt
        body, content_type = _encode_multipart(
            fields,
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

    def _cli_transcribe(self, wav: bytes, prompt: Optional[str] = None) -> str:
        if not self._cli:
            raise RuntimeError("no local whisper binary available")
        with tempfile.TemporaryDirectory(prefix="zoomrec_stt_") as tmp:
            wav_path = Path(tmp) / "chunk.wav"
            out_base = Path(tmp) / "out"
            wav_path.write_bytes(wav)
            cmd = [self._cli, "-m", str(self.model), "-f", str(wav_path),
                   "-otxt", "-of", str(out_base), "-np"]
            if prompt:
                cmd += ["--prompt", prompt]
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

    Each source gets its own ffmpeg tap, chunker, a bounded queue and a
    dedicated transcription worker, so a slow network call never blocks reading
    audio (which would make the tap drift behind real time). Speech can also be
    attributed to the channel it arrived on -- no diarization model needed for
    the two-party case.
    """

    def __init__(self, speaker: Optional[str], cmd: List[str]) -> None:
        self.speaker = speaker
        self.cmd = cmd
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.worker: Optional[threading.Thread] = None
        self.partial_worker: Optional[threading.Thread] = None
        self.queue: "queue.Queue" = queue.Queue(maxsize=4)
        self.partial_queue: "queue.Queue" = queue.Queue(maxsize=1)
        self.partial_lock = threading.Lock()
        self.partial_buffer = bytearray()
        self.partial_last_submit = 0.0
        self.partial_decoder = StablePartialDecoder()
        self.partial_revision = 0
        self.vad = None
        self.tail: List[str] = []
        self.context_tail = ""
        self.dropped = 0
        self.transcribed = 0
        self.segment_seq = 0
        self.last_segment_id = ""


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
        self._glossary = [g for g in getattr(cfg, "stt_glossary", []) if g]
        self._provider_backoff_until = 0.0
        self._provider_backoff_seconds = 4.0
        self._provider_backoff_lock = threading.Lock()
        # whisper-server is effectively a single local inference lane. Final
        # chunks wait for it; stale interim frames skip it and are replaced by
        # the next rolling window.
        self._inference_lock = threading.Lock()
        self._partial_dropped = 0
        self._final_inference_count = 0
        self._active_chunk_seconds = float(cfg.stt_chunk_seconds)

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
        self._start_sources()

    def update_devices(self, mic_name: Optional[str], system_name: Optional[str]) -> None:
        """Follow a recorder device switch without restarting the whole HUD."""
        if (mic_name, system_name) == (self.mic_name, self.system_name):
            return
        self.log("live HUD: devices changed (mic='{}', system='{}')".format(
            mic_name or "?", system_name or "none"))
        self._stop_sources(self._sources)
        self._sources = []
        self.mic_name = mic_name
        self.system_name = system_name
        if self._stop.is_set() or self._stt is None:
            return
        try:
            self._sources = self._build_sources()
        except Exception as exc:  # noqa: BLE001
            self.log("live HUD: could not rebuild sources ({})".format(exc))
            return
        self._start_sources()

    def _start_sources(self) -> None:
        label = " + ".join(s.speaker or "mixed" for s in self._sources)
        self._active_chunk_seconds = self._effective_chunk_seconds()
        self.log("live STT running ({} backend, {:.0f}s chunks, {} queue, sources: {})".format(
            self.cfg.stt_backend, self._active_chunk_seconds,
            getattr(self.cfg, "stt_queue_chunks", 4), label))
        if self._glossary:
            self.log("live STT glossary: {}".format(", ".join(self._glossary)))
        for source in self._sources:
            source.queue = queue.Queue(maxsize=max(1, int(getattr(self.cfg, "stt_queue_chunks", 4))))
            source.worker = threading.Thread(
                target=self._stt_worker, args=(source,),
                name="hud-stt-work-{}".format(source.speaker or "mix"), daemon=True)
            source.worker.start()
            if self._partial_enabled():
                source.partial_queue = queue.Queue(maxsize=1)
                source.partial_worker = threading.Thread(
                    target=self._partial_worker, args=(source,),
                    name="hud-stt-partial-{}".format(source.speaker or "mix"), daemon=True)
                source.partial_worker.start()
            source.thread = threading.Thread(
                target=self._run_source, args=(source,),
                name="hud-stt-{}".format(source.speaker or "mix"), daemon=True)
            source.thread.start()

    def _effective_chunk_seconds(self) -> float:
        """Avoid exceeding Groq's current STT request rate with two sources."""
        configured = float(self.cfg.stt_chunk_seconds)
        if ((self.cfg.stt_backend or "").lower() == "groq"
                and len(self._sources) >= 2 and configured < 7.0):
            self.log("live STT: using 7s chunks for two Groq sources to stay below "
                     "the provider request limit (configured {:.1f}s)".format(configured))
            return 7.0
        return configured

    def _partial_enabled(self) -> bool:
        return ((self.cfg.stt_backend or "").lower() in LOCAL_STT_BACKENDS
                and bool(getattr(self.cfg, "stt_partial_enabled", True))
                and float(getattr(self.cfg, "stt_partial_window_seconds", 2.0)) > 0)

    def _stop_sources(self, sources: List["_Source"]) -> None:
        for source in sources:
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
        for source in sources:
            if source.thread is not None:
                source.thread.join(timeout=8.0)
        for source in sources:
            try:
                source.queue.put_nowait(None)
            except queue.Full:
                try:
                    source.queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    source.queue.put_nowait(None)
                except queue.Full:
                    pass
            try:
                source.partial_queue.put_nowait(None)
            except queue.Full:
                try:
                    source.partial_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    source.partial_queue.put_nowait(None)
                except queue.Full:
                    pass
        for source in sources:
            if source.worker is not None:
                source.worker.join(timeout=8.0)
            if source.partial_worker is not None:
                source.partial_worker.join(timeout=8.0)

    def stop(self) -> None:
        self._stop.set()
        self._stop_sources(self._sources)
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
        return RemoteSTT(
            client, model, self.log, verbose=self.cfg.stt_verbose_stt,
            no_speech_prob_max=self.cfg.stt_no_speech_prob_max,
            avg_logprob_min=self.cfg.stt_avg_logprob_min,
            compression_ratio_max=self.cfg.stt_compression_ratio_max)

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
        source.vad = build_vad(self.cfg, self.log)
        chunker = Chunker(self._active_chunk_seconds, self.cfg.stt_min_speech_seconds,
                          vad=source.vad)
        assert source.proc.stdout is not None
        last_publish = 0.0
        try:
            for block in self._iter_blocks(source.proc.stdout):
                self._feed_partial(source, block)
                for chunk in chunker.feed(block):
                    self._enqueue(source, chunk, chunker.last_margin_db)
                # Publish the live level even when the gate is rejecting
                # everything, so the HUD can show why nothing is transcribed.
                now = time.time()
                if now - last_publish >= 2.0:
                    last_publish = now
                    self._publish_lag()
            tail = chunker.flush()
            if tail:
                self._enqueue(source, tail, chunker.last_margin_db)
        except Exception as exc:  # noqa: BLE001
            self.log("live transcription loop stopped ({}): {}".format(
                source.speaker or "mixed", exc))

    def _iter_blocks(self, stream, block_bytes: int = 6400) -> Iterator[bytes]:
        while not self._stop.is_set():
            data = stream.read(block_bytes)
            if not data:
                return
            yield data

    def _enqueue(self, source: _Source, chunk: bytes, margin_db: float = 0.0) -> None:
        if self._stop.is_set():
            return
        item = (time.time(), chunk, margin_db)
        try:
            source.queue.put_nowait(item)
        except queue.Full:
            # Backpressure: drop the oldest queued chunk so the tap stays live
            # rather than drifting further and further behind.
            try:
                source.queue.get_nowait()
            except queue.Empty:
                pass
            source.dropped += 1
            try:
                source.queue.put_nowait(item)
            except queue.Full:
                pass
        self._publish_lag()

    def _publish_lag(self) -> None:
        queued = sum(s.queue.qsize() for s in self._sources)
        dropped = sum(s.dropped for s in self._sources)
        lag = round(queued * self._active_chunk_seconds, 1)
        meta = {"stt_lag_seconds": lag, "stt_queued": queued, "stt_dropped": dropped}
        thresholds = [t for t in (
            getattr(s.vad, "threshold_db", lambda: None)()
            for s in self._sources if s.vad is not None) if t is not None]
        levels = [s.vad.last_level_db for s in self._sources
                  if getattr(s.vad, "last_level_db", None) is not None]
        if thresholds:
            meta["stt_vad_threshold_db"] = round(max(thresholds), 1)
        if levels:
            meta["stt_vad_level_db"] = round(max(levels), 1)
        self.state.set_meta(**meta)

    def _stt_worker(self, source: _Source) -> None:
        while not self._stop.is_set():
            try:
                item = source.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            captured_at, chunk, margin_db = item
            self._transcribe_chunk(chunk, source, captured_at, margin_db)

    def _feed_partial(self, source: "_Source", block: bytes) -> None:
        """Queue the newest short rolling window without blocking capture."""
        if not self._partial_enabled():
            return
        window_bytes = int(float(getattr(self.cfg, "stt_partial_window_seconds", 2.0))
                          * SAMPLE_RATE * SAMPLE_WIDTH)
        if window_bytes <= 0:
            return
        now = time.time()
        with source.partial_lock:
            source.partial_buffer.extend(block)
            max_bytes = window_bytes * 3
            if len(source.partial_buffer) > max_bytes:
                del source.partial_buffer[:-max_bytes]
            if (len(source.partial_buffer) < window_bytes
                    or now - source.partial_last_submit
                    < float(getattr(self.cfg, "stt_partial_interval_seconds", 0.8))):
                return
            source.partial_last_submit = now
            audio = bytes(source.partial_buffer[-window_bytes:])
        try:
            source.partial_queue.put_nowait((now, audio))
        except queue.Full:
            # A slow inference is allowed to skip an interim frame; the next
            # frame always contains newer audio and keeps the UI current.
            pass

    def _partial_worker(self, source: "_Source") -> None:
        while not self._stop.is_set():
            try:
                item = source.partial_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            captured_at, audio = item
            started = time.time()
            try:
                if not self._inference_lock.acquire(blocking=False):
                    self._partial_dropped += 1
                    self.state.set_meta(stt_partial_dropped=self._partial_dropped)
                    continue
                try:
                    out = self._stt.transcribe(  # type: ignore[attr-defined]
                        audio, prompt=self._prompt_for(source))
                finally:
                    self._inference_lock.release()
                result = out if isinstance(out, STTResult) else STTResult(text=str(out or ""))
                text = result.text.strip()
                if (text and (not self.cfg.stt_hallucination_filter
                              or not looks_hallucinated(text, marginal=True))):
                    committed = source.partial_decoder.accept(text)
                    source.partial_revision += 1
                    provisional = source.partial_decoder.provisional_text
                    if committed:
                        self._publish_committed(source, committed, started, finalized=False)
                    self.state.set_transcript_partial(
                        source.speaker or "mixed", provisional, source.speaker,
                        source.partial_revision,
                        latency=round(time.time() - started, 2),
                        captured_at=captured_at)
                    self.state.set_meta(
                        stt_partial_latency=round(max(0.0, time.time() - captured_at), 2))
                    self.state.observe_metric("stt_partial_latency_seconds",
                                              max(0.0, time.time() - captured_at))
            except Exception as exc:  # noqa: BLE001
                # Partial recognition is an enhancement. A failed interim
                # request must never disable final transcription.
                self.log("local STT partial frame skipped: {}".format(exc))

    def _publish_committed(self, source: "_Source", text: str,
                           started: Optional[float] = None,
                           finalized: bool = True) -> None:
        delta = self._delta_text(source, text)
        if not delta:
            return
        source.context_tail = (source.context_tail + " " + delta).strip()[-200:]
        source.transcribed += 1
        source.segment_seq += 1
        source.last_segment_id = "{}:{}".format(
            source.speaker or "mixed", source.segment_seq)
        self.state.add("transcript", text=delta, source="live",
                       speaker=source.speaker,
                       latency=round(time.time() - started, 2) if started else None,
                       stable_partial=not finalized, finalized=finalized,
                       segment_id=source.last_segment_id,
                       revision=source.partial_revision,
                       captured_at=started)
        if not finalized:
            self.state.add("transcript_revision", text=delta, source="live",
                           speaker=source.speaker, segment_id=source.last_segment_id,
                           revision=source.partial_revision, finalized=False)

    def _prompt_for(self, source: _Source) -> Optional[str]:
        if not self.cfg.stt_context_prompt:
            return None
        parts: List[str] = []
        if self._glossary:
            parts.append(", ".join(self._glossary))
        context = source.context_tail
        if context:
            # End on a sentence boundary: an incomplete prompt encourages
            # Whisper to "continue" it (a common hallucination trigger).
            match = None
            for match in re.finditer(r"[.!?]", context):
                pass
            if match is not None:
                context = context[: match.end()]
            else:
                context = ""
        if context:
            parts.append(context)
        prompt = " ".join(parts).strip()
        # Whisper prompts are short; keep well inside the token budget.
        return prompt[-600:] if prompt else None

    def _transcribe_chunk(self, chunk: bytes, source: "_Source",
                          captured_at: Optional[float] = None,
                          margin_db: float = 0.0) -> None:
        if self._stop.is_set():
            return
        if (self.cfg.stt_backend or "").lower() not in LOCAL_STT_BACKENDS:
            with self._provider_backoff_lock:
                blocked = self._provider_backoff_until - time.time()
            if blocked > 0:
                self.state.set_meta(stt_provider_backoff_seconds=round(blocked, 1))
                return
        started = time.time()
        prompt = self._prompt_for(source)
        try:
            # Final recognition has priority over interim recognition, and the
            # lock keeps two independent sources from contending in the same
            # whisper-server process.
            with self._inference_lock:
                try:
                    out = self._stt.transcribe(chunk, prompt=prompt)  # type: ignore[attr-defined]
                except TypeError:
                    out = self._stt.transcribe(chunk)  # type: ignore[attr-defined]
                self._final_inference_count += 1
        except LLMError as exc:
            self.log("STT error: {}".format(exc))
            self.state.add("status", status="recording", stt_error=str(exc))
            if exc.status == 429:
                with self._provider_backoff_lock:
                    delay = max(2.0, min(60.0, exc.retry_after or
                                         self._provider_backoff_seconds))
                    self._provider_backoff_until = time.time() + delay
                    self._provider_backoff_seconds = min(60.0, delay * 2.0)
                self.log("live STT: provider rate limited; dropping queued audio for "
                         "{:.0f}s to stay live".format(delay))
            return
        except Exception as exc:  # noqa: BLE001
            self.log("STT failure: {}".format(exc))
            return
        if (self.cfg.stt_backend or "").lower() not in LOCAL_STT_BACKENDS:
            with self._provider_backoff_lock:
                self._provider_backoff_until = 0.0
                self._provider_backoff_seconds = 4.0
        result = out if isinstance(out, STTResult) else STTResult(text=str(out or ""))
        text = result.text.strip()
        if not text:
            return
        if self.cfg.stt_hallucination_filter and looks_hallucinated(
                text, marginal=margin_db < self.cfg.stt_vad_margin_db,
                avg_logprob=result.avg_logprob, no_speech_prob=result.no_speech_prob,
                compression_ratio=result.compression_ratio,
                no_speech_prob_max=self.cfg.stt_no_speech_prob_max,
                avg_logprob_min=self.cfg.stt_avg_logprob_min,
                compression_ratio_max=self.cfg.stt_compression_ratio_max):
            self.log("live STT: dropped likely hallucination ({!r}...)".format(text[:40]))
            return
        self._publish_committed(source, text, started, finalized=True)
        if self._partial_enabled():
            self.state.add("transcript_boundary", source_key=source.speaker or "mixed",
                           speaker=source.speaker, finalized=True,
                           segment_id=source.last_segment_id,
                           revision=source.partial_revision + 1)
            source.partial_revision += 1
            self.state.set_transcript_partial(
                source.speaker or "mixed", "", source.speaker,
                source.partial_revision, finalized=True)
        if captured_at is not None:
            self.state.observe_metric("stt_final_latency_seconds",
                                      max(0.0, time.time() - captured_at))
            self.state.set_meta(
                stt_lag_seconds=round(max(0.0, time.time() - captured_at), 1),
                stt_final_inferences=self._final_inference_count,
                stt_partial_dropped=self._partial_dropped)

    def _delta_text(self, source: "_Source", text: str) -> str:
        words = split_words(text)
        if not words:
            return ""
        k = fuzzy_overlap(source.tail, words)
        new_words = words[k:]
        source.tail = (source.tail + new_words)[-12:]
        return " ".join(new_words)
