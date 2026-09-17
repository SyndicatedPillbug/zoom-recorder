#!/usr/bin/env python3
"""Voice-activity detection for the live tap.

Steady broadband noise (air conditioning, fan hum) sits well above a fixed
peak threshold, so an absolute gate happily transcribes it -- and Whisper then
hallucinates repetitive filler on that non-speech audio. Instead we track a
per-source adaptive noise floor and only treat energy clearly above it as
speech.

Detection uses *peak* level, which is what the recorder's original gate used
and is far more forgiving of quiet call/loopback audio than RMS. ``webrtcvad``
is used for a real VAD when installed; otherwise the stdlib energy detector is
used, so nothing new is required.
"""

from __future__ import annotations

import math
import struct
from typing import Callable, List, Optional

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2


def frame_level_dbfs(pcm: bytes) -> float:
    """Peak level in dBFS for a small PCM block (0 dB == full scale).

    Peak (not RMS) keeps the gate sensitive to quiet speech, matching the
    recorder's original behaviour; the adaptive floor handles steady noise.
    """
    count = len(pcm) // SAMPLE_WIDTH
    if count <= 0:
        return -120.0
    samples = struct.unpack("<{}h".format(count), pcm[: count * SAMPLE_WIDTH])
    peak = max(abs(sample) for sample in samples)
    if peak == 0:
        return -120.0
    return 20.0 * math.log10(peak / 32768.0)


class NoiseFloor:
    """Adaptive ambient level in dBFS.

    Falls quickly toward quiet frames and rises slowly, so a steady hum becomes
    the floor while speech (a clear, sustained boost above it) stays speech.
    """

    def __init__(self, initial_db: float = -60.0, rise_db_per_frame: float = 0.3,
                 floor_db: float = -80.0, ceiling_db: float = -20.0) -> None:
        self.noise_db = max(floor_db, min(ceiling_db, initial_db))
        self.rise_db_per_frame = rise_db_per_frame
        self.floor_db = floor_db
        self.ceiling_db = ceiling_db

    def update(self, level_db: float, margin_db: float) -> float:
        if level_db < self.noise_db:
            self.noise_db = level_db
        elif level_db < self.noise_db + margin_db:
            # Close enough to the floor to be ambient: let it creep up so a
            # newly-started hum is eventually absorbed.
            self.noise_db += self.rise_db_per_frame
        self.noise_db = max(self.floor_db, min(self.ceiling_db, self.noise_db))
        return self.noise_db


class EnergyVAD:
    """Adaptive energy gate with a short startup calibration."""

    label = "energy"

    def __init__(self, absolute_db: float = -50.0, margin_db: float = 6.0,
                 adaptive: bool = True, calibration_frames: int = 10,
                 max_rise_db: float = 30.0) -> None:
        self.absolute_db = absolute_db
        self.margin_db = margin_db
        self.adaptive = adaptive
        self.calibration_frames = max(0, calibration_frames)
        self.max_noise_db = absolute_db + max_rise_db
        self._noise = NoiseFloor(initial_db=absolute_db)
        self._calibrated = not adaptive or self.calibration_frames == 0
        self._calibration: List[float] = []
        self.last_margin_db = 0.0
        self.last_level_db = -120.0

    def threshold_db(self) -> float:
        if not self.adaptive:
            return self.absolute_db
        floor = min(self._noise.noise_db, self.max_noise_db)
        return max(self.absolute_db, floor + self.margin_db)

    def is_speech(self, frame: bytes) -> bool:
        level = frame_level_dbfs(frame)
        self.last_level_db = level
        if not self._calibrated:
            # Ignore the first fraction of a second entirely: assume it is
            # ambient. The quietest frame is the safest floor estimate -- if
            # speech was already happening it is an inter-word gap, not the
            # speech itself, so we never calibrate the gate above the talker.
            self._calibration.append(level)
            self.last_margin_db = 0.0
            if len(self._calibration) >= self.calibration_frames:
                floor = min(self._calibration)
                self._noise.noise_db = max(self._noise.floor_db,
                                           min(self.max_noise_db, floor))
                self._calibrated = True
            return False

        threshold = self.threshold_db()
        speech = level > threshold
        self.last_margin_db = level - threshold
        self._noise.update(level, self.margin_db)
        return speech


class WebRTCVAD:
    """Real VAD via the optional ``webrtcvad`` package (20 ms sub-frames)."""

    label = "webrtcvad"

    def __init__(self, aggressiveness: int = 1) -> None:
        import webrtcvad  # lazy, optional

        self._vad = webrtcvad.Vad(int(aggressiveness))
        self.frame_ms = 20
        self.frame_bytes = int(SAMPLE_RATE * SAMPLE_WIDTH * self.frame_ms / 1000)
        self.last_margin_db = 0.0
        self.last_level_db = -120.0

    def threshold_db(self) -> Optional[float]:
        return None

    def is_speech(self, frame: bytes) -> bool:
        self.last_level_db = frame_level_dbfs(frame)
        if len(frame) < self.frame_bytes:
            return False
        votes = 0
        total = 0
        for start in range(0, len(frame) - self.frame_bytes + 1, self.frame_bytes):
            total += 1
            try:
                if self._vad.is_speech(frame[start:start + self.frame_bytes], SAMPLE_RATE):
                    votes += 1
            except Exception:  # noqa: BLE001
                continue
        return total > 0 and votes * 2 >= total


def build_vad(cfg, log: Callable[[str], None]):
    """Return a VAD according to ``stt.vad_backend`` (default: auto)."""
    backend = (getattr(cfg, "stt_vad_backend", "auto") or "auto").lower()
    if backend in ("auto", "webrtcvad"):
        try:
            vad = WebRTCVAD(int(getattr(cfg, "stt_vad_aggressiveness", 1)))
            log("live STT: speech detection using webrtcvad (aggressiveness {})".format(
                getattr(cfg, "stt_vad_aggressiveness", 1)))
            return vad
        except Exception as exc:  # noqa: BLE001
            if backend == "webrtcvad":
                log("live STT: webrtcvad unavailable ({}); using energy VAD".format(exc))
    vad = EnergyVAD(
        absolute_db=float(getattr(cfg, "stt_silence_db", -50.0)),
        margin_db=float(getattr(cfg, "stt_vad_margin_db", 6.0)),
        adaptive=bool(getattr(cfg, "stt_adaptive_vad", True)),
    )
    log("live STT: speech detection using adaptive energy VAD (floor {:.0f} dB, "
        "margin {:.0f} dB)".format(vad.absolute_db, vad.margin_db))
    return vad
