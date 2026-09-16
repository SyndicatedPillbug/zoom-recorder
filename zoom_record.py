#!/usr/bin/env python3
"""Robust Zoom + microphone recorder with automatic device selection,
continuous capture health testing, dynamic failover, and post-recording
verification.

How it behaves:
  * Enumerates every avfoundation audio device by NAME (indices are unstable).
  * Picks the best mic: prefers a device that is actually delivering signal,
    then falls back to a quality ranking (wired external > built-in > BT).
  * Records mic and system (loopback) audio as SEPARATE mono tracks, segmented
    for crash safety. They are never pre-mixed -- boosting/denoising one side
    later requires them to stay apart.
  * Every --chunk-seconds it probes the active mic. Digital silence reads about
    -91 dB, so --silence-db (default -60) cleanly separates a dead input from a
    live one.
  * After --fail-threshold consecutive silent probes it cycles through every
    other candidate and switches to the best one that has signal. The current
    capture keeps running the whole time, so natural meeting silence never
    drops audio.
  * Every --cycle-seconds it probes all inactive inputs and logs a level table.
  * On stop: merges each track's segments, VERIFIES the merge (duration sanity
    + signal-coverage scan), archives the raw segments (never deletes them),
    makes the merged originals read-only, appends a checksum manifest entry,
    then builds a mixed copy under derived/ for transcription only.

Usage:
    ./zoom-record.sh [minutes]
    ./zoom_record.py [--minutes 5] [--mic NAME] [--system NAME] [--list]
    ./zoom_record.py --self-test       # verify the system-audio loopback path
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_MODEL = "~/.cache/whisper-cpp/ggml-base.en.bin"
FLOOR_DB = -91.0
LOW_COVERAGE_PCT = 50.0
DURATION_TOLERANCE_PCT = 0.02
DURATION_TOLERANCE_MIN_S = 2.0
LONG_SILENCE_S = 60.0

LOOPBACK_RE = re.compile(
    r"zoom\s*audio\s*device|blackhole|black\s*hole|loopback|soundflower|"
    r"multi-?output|aggregate|virtual",
    re.I,
)

MIC_PRIORITY = [
    (re.compile(r"external|usb|yet[ai]|blue\b|rode|shure|audio[\s-]?technica|samson|fifine|elgato|logitech", re.I), 60),
    (re.compile(r"built-?in|macbook|imac|studio display|display", re.I), 50),
    (re.compile(r"headset|airpods|beats|headphone|bluetooth", re.I), 30),
    (re.compile(r"iphone|continuity|desk view", re.I), 10),
]

SYSTEM_PRIORITY = [
    (re.compile(r"zoom\s*audio\s*device", re.I), 100),
    (re.compile(r"blackhole", re.I), 90),
    (re.compile(r"loopback", re.I), 80),
    (re.compile(r"soundflower", re.I), 70),
    (re.compile(r"aggregate|multi-?output", re.I), 60),
]

DEVICE_RE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")
MEAN_RE = re.compile(r"mean_volume:\s*(-?[\d.]+)\s*dB")
MAX_RE = re.compile(r"max_volume:\s*(-?[\d.]+)\s*dB")
SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)")


class Log:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self._fh = None
        if path:
            self._fh = open(path, "a", encoding="utf-8")

    def _emit(self, level: str, msg: str) -> None:
        line = "[{}] {:5} {}".format(datetime.now().strftime("%H:%M:%S"), level, msg)
        print(line, flush=True)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def info(self, msg: str) -> None:
        self._emit("INFO", msg)

    def warn(self, msg: str) -> None:
        self._emit("WARN", msg)

    def error(self, msg: str) -> None:
        self._emit("ERROR", msg)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


@dataclass
class Device:
    index: int
    name: str
    kind: str  # "input" | "output"

    def __str__(self) -> str:
        return self.name


@dataclass
class Probe:
    ok: bool
    mean_db: Optional[float]
    max_db: Optional[float]
    error: str = ""


@dataclass
class Candidate:
    device: Device
    priority: int
    probe: Optional[Probe] = None


@dataclass
class VerifyResult:
    ok: bool
    duration_s: float
    expected_duration_s: Optional[float]
    mean_db: Optional[float]
    max_db: Optional[float]
    coverage_pct: float
    long_silences: List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class Config:
    segment_seconds: int
    chunk_seconds: float
    fail_threshold: int
    cycle_seconds: float
    probe_seconds: float
    silence_db: float
    mic_override: Optional[str]
    system_override: Optional[str]
    use_system: bool
    transcribe: bool
    model: Path
    basedir: Path
    outdir: Path
    workdir: Path


def fmt_db(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value <= FLOOR_DB + 1:
        return "-inf"
    return "{:.1f} dB".format(value)


def has_signal(probe: Probe, threshold: float) -> bool:
    return bool(probe.ok and probe.max_db is not None and probe.max_db > threshold)


def list_devices() -> Tuple[List[Device], List[Device]]:
    cmd = ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    inputs: List[Device] = []
    outputs: List[Device] = []
    section: Optional[str] = None
    for line in proc.stderr.splitlines():
        if "AVFoundation video devices:" in line:
            section = "video"
            continue
        if "AVFoundation audio devices:" in line:
            section = "input"
            continue
        if "AVFoundation audio output devices:" in line:
            section = "output"
            continue
        if section not in ("input", "output"):
            continue
        match = DEVICE_RE.search(line)
        if match:
            dev = Device(int(match.group(1)), match.group(2).strip(), section)
            (inputs if section == "input" else outputs).append(dev)
    return inputs, outputs


def probe_level(device: Device, seconds: float) -> Probe:
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin",
        "-f", "avfoundation", "-i", ":{}".format(device.name),
        "-t", str(seconds),
        "-af", "volumedetect",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 12)
    except subprocess.TimeoutExpired:
        return Probe(False, None, None, "probe timed out")
    err = proc.stderr
    mean_m = MEAN_RE.search(err)
    max_m = MAX_RE.search(err)
    if max_m is None:
        tail = err.strip().splitlines()[-1] if err.strip() else "no output"
        return Probe(False, None, None, tail)
    mean = float(mean_m.group(1)) if mean_m else None
    return Probe(True, mean, float(max_m.group(1)), "")


def mic_priority(name: str) -> int:
    for pattern, score in MIC_PRIORITY:
        if pattern.search(name):
            return score
    return 20


def system_priority(name: str) -> int:
    for pattern, score in SYSTEM_PRIORITY:
        if pattern.search(name):
            return score
    return 0


def build_mic_candidates(inputs: List[Device]) -> List[Candidate]:
    candidates = []
    for dev in inputs:
        if LOOPBACK_RE.search(dev.name):
            continue
        candidates.append(Candidate(dev, mic_priority(dev.name)))
    candidates.sort(key=lambda c: (-c.priority, c.device.index))
    return candidates


def build_system_candidates(inputs: List[Device]) -> List[Candidate]:
    candidates = [Candidate(d, system_priority(d.name)) for d in inputs if LOOPBACK_RE.search(d.name)]
    candidates.sort(key=lambda c: (-c.priority, c.device.index))
    return candidates


def find_device(inputs: List[Device], name: str) -> Optional[Device]:
    for dev in inputs:
        if dev.name == name:
            return dev
    return None


def choose_mic(cfg: Config, candidates: List[Candidate], log: Log,
               exclude: Optional[str] = None) -> Optional[Candidate]:
    if cfg.mic_override:
        dev = find_device([c.device for c in candidates], cfg.mic_override)
        if dev is None:
            for d in [c.device for c in candidates]:
                if cfg.mic_override.lower() in d.name.lower():
                    dev = d
                    break
        if dev is None:
            log.error("Requested mic '{}' not found.".format(cfg.mic_override))
            return None
        return Candidate(dev, mic_priority(dev.name), probe_level(dev, cfg.probe_seconds))

    for cand in candidates:
        if exclude and cand.device.name == exclude:
            continue
        if cand.probe is None:
            cand.probe = probe_level(cand.device, cfg.probe_seconds)

    usable = [c for c in candidates if c.probe and c.probe.ok]
    if not usable:
        usable = [c for c in candidates if not (c.probe and not c.probe.ok)]
    if not usable:
        return None

    log.info("Mic candidates:")
    for cand in candidates:
        p = cand.probe
        detail = fmt_db(p.max_db) if p and p.ok else "unavailable ({})".format(p.error if p else "not probed")
        log.info("  - {:<32} priority={:<3} level={}".format(cand.device.name, cand.priority, detail))

    usable.sort(key=lambda c: (not has_signal(c.probe, cfg.silence_db), -c.priority, c.device.index))
    return usable[0]


def choose_system(cfg: Config, candidates: List[Candidate], log: Log,
                  exclude: Optional[str] = None) -> Optional[Candidate]:
    if cfg.system_override:
        names = [c.device for c in candidates]
        dev = find_device(names, cfg.system_override)
        if dev is None:
            all_inputs = list_devices()[0]
            dev = find_device(all_inputs, cfg.system_override)
        if dev is None:
            log.warn("Requested system device '{}' not found.".format(cfg.system_override))
            return None
        return Candidate(dev, system_priority(dev.name))

    for cand in candidates:
        if exclude and cand.device.name == exclude:
            continue
        if cand.probe is None:
            cand.probe = probe_level(cand.device, cfg.probe_seconds)
        if cand.probe.ok:
            return cand
    return None


class Recorder:
    """Records mic and system audio as separate mono tracks (never mixed) so
    each source can be archived, verified, and later processed independently.
    """

    def __init__(self, cfg: Config, log: Log) -> None:
        self.cfg = cfg
        self.log = log
        self.proc: Optional[subprocess.Popen] = None
        self.mic: Optional[Candidate] = None
        self.system: Optional[Candidate] = None
        self.sessions: List[Path] = []
        self._session = 0
        self._stderr_fh = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, mic: Device, system: Optional[Device]) -> None:
        self.stop()
        self._session += 1
        sdir = self.cfg.workdir / "session_{:04d}".format(self._session)
        sdir.mkdir(parents=True, exist_ok=True)
        self.sessions.append(sdir)
        mic_pattern = str(sdir / "seg_%05d_mic.wav")
        sys_pattern = str(sdir / "seg_%05d_sys.wav")

        cmd = [
            "ffmpeg", "-hide_banner", "-thread_queue_size", "1024",
            "-f", "avfoundation", "-i", ":{}".format(mic.name),
        ]
        if system is not None:
            cmd += [
                "-thread_queue_size", "1024",
                "-f", "avfoundation", "-i", ":{}".format(system.name),
            ]
        segment_opts = [
            "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le",
            "-f", "segment",
            "-segment_time", str(int(self.cfg.segment_seconds)),
            "-reset_timestamps", "1",
        ]
        cmd += ["-map", "0:a"] + segment_opts + [mic_pattern]
        if system is not None:
            cmd += ["-map", "1:a"] + segment_opts + [sys_pattern]

        self._stderr_fh = open(sdir / "ffmpeg.log", "ab")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._stderr_fh
        )
        self.mic = Candidate(mic, mic_priority(mic.name))
        self.system = Candidate(system, 0) if system else None
        src = "{}".format(mic.name)
        if system is not None:
            src += " + {} (separate tracks)".format(system.name)
        self.log.info("Recording session {} -> {}".format(self._session, src))

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.write(b"q")
                    self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass
        if self._stderr_fh:
            self._stderr_fh.close()
            self._stderr_fh = None
        self.proc = None

    def segments_mic(self) -> List[Path]:
        segs: List[Path] = []
        for sdir in self.sessions:
            segs.extend(sorted(sdir.glob("seg_*_mic.wav")))
        return segs

    def segments_sys(self) -> List[Path]:
        segs: List[Path] = []
        for sdir in self.sessions:
            segs.extend(sorted(sdir.glob("seg_*_sys.wav")))
        return segs


def cycle_probe(cfg: Config, mic_cands: List[Candidate], system_cands: List[Candidate],
                active_mic: Optional[str], active_system: Optional[str], log: Log) -> None:
    log.info("Periodic input cycle test:")
    for cand in mic_cands:
        if cand.device.name == active_mic:
            continue
        p = probe_level(cand.device, cfg.probe_seconds)
        log.info("  - {:<32} {} ({})".format(
            cand.device.name, fmt_db(p.max_db) if p.ok else "open failed", "MIC"))
    for cand in system_cands:
        if cand.device.name == active_system:
            continue
        p = probe_level(cand.device, cfg.probe_seconds)
        log.info("  - {:<32} {} ({})".format(
            cand.device.name, fmt_db(p.max_db) if p.ok else "open failed", "SYSTEM"))


def hunt_mic(cfg: Config, candidates: List[Candidate], active: Optional[str], log: Log) -> Optional[Device]:
    best: Optional[Tuple[bool, int, int, Device]] = None
    for cand in candidates:
        if cand.device.name == active:
            continue
        p = probe_level(cand.device, cfg.probe_seconds)
        signal = has_signal(p, cfg.silence_db)
        log.info("  hunt {:<32} {} {}".format(
            cand.device.name, fmt_db(p.max_db) if p.ok else "open failed",
            "<- signal" if signal else ""))
        key = (signal, cand.priority, -cand.device.index, cand.device)
        if signal and (best is None or key[:3] > best[:3]):
            best = key
    return best[3] if best else None


def monitor(cfg: Config, rec: Recorder, mic_cands: List[Candidate],
            system_cands: List[Candidate], stop: threading.Event, log: Log) -> None:
    silent = 0
    last_cycle = time.monotonic()
    while not stop.wait(cfg.chunk_seconds):
        if not rec.is_alive():
            log.error("Recorder exited unexpectedly; restarting.")
            new_mic = choose_mic(cfg, mic_cands, log)
            if new_mic is None:
                log.error("No usable mic; cannot restart.")
                stop.set()
                break
            new_system = rec.system.device if rec.system else None
            rec.start(new_mic.device, new_system)
            silent = 0
            continue

        mic_probe = probe_level(rec.mic.device, cfg.probe_seconds)
        if has_signal(mic_probe, cfg.silence_db):
            if silent:
                log.info("Mic '{}' recovered.".format(rec.mic.device.name))
            silent = 0
        else:
            silent += 1
            log.warn("Mic '{}' silent ({}) — {}/{} before failover.".format(
                rec.mic.device.name, fmt_db(mic_probe.max_db) if mic_probe.ok else "open failed",
                silent, cfg.fail_threshold))

        if silent >= cfg.fail_threshold:
            log.warn("Mic failed health checks; cycling all inputs for signal...")
            best = hunt_mic(cfg, mic_cands, rec.mic.device.name, log)
            if best is not None:
                log.warn("Switching mic: '{}' -> '{}'".format(rec.mic.device.name, best.name))
                rec.start(best, rec.system.device if rec.system else None)
                silent = 0
            else:
                log.warn("No candidate produced signal; keeping capture running "
                         "(meeting may simply be quiet).")
                silent = 0

        if rec.system is not None:
            sys_probe = probe_level(rec.system.device, cfg.probe_seconds)
            if not sys_probe.ok:
                log.warn("System input '{}' failed to open; cycling...".format(rec.system.device.name))
                new_sys = choose_system(cfg, system_cands, log, exclude=rec.system.device.name)
                if new_sys is not None:
                    log.warn("Switching system input: '{}' -> '{}'".format(
                        rec.system.device.name, new_sys.device.name))
                    rec.start(rec.mic.device, new_sys.device)
                else:
                    log.warn("No replacement system input available; continuing without it.")
                    rec.start(rec.mic.device, None)

        now = time.monotonic()
        if now - last_cycle >= cfg.cycle_seconds:
            last_cycle = now
            cycle_probe(cfg, mic_cands, system_cands,
                        rec.mic.device.name if rec.mic else None,
                        rec.system.device.name if rec.system else None, log)


def merge_segments(segments: List[Path], out_path: Path, workdir: Path, log: Log) -> bool:
    if not segments:
        return False
    if len(segments) == 1:
        shutil.copy(segments[0], out_path)
        return True
    concat_list = workdir / "concat_{}.txt".format(out_path.stem)
    with open(concat_list, "w", encoding="utf-8") as fh:
        for seg in segments:
            fh.write("file '{}'\n".format(seg))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0",
           "-i", str(concat_list), "-c", "copy", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("Merge failed: {}".format(proc.stderr.strip().splitlines()[-1] if proc.stderr else "unknown"))
        return False
    return True


def probe_duration(path: Path) -> Optional[float]:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(proc.stdout.strip())
    except (ValueError, TypeError):
        return None


def alert(label: str, coverage: float) -> None:
    """Loud, immediate signal that a recording came out mostly silent --
    this exact failure mode sat unnoticed in a transcript for weeks before."""
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass
    try:
        message = "{} track coverage: {:.0f}% (below {:.0f}% threshold)".format(
            label, coverage, LOW_COVERAGE_PCT)
        subprocess.run(
            ["osascript", "-e",
             'display notification "{}" with title "zoom-recorder: low signal" sound name "Basso"'.format(message)],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def verify_recording(merged: Path, segments: List[Path], log: Log, label: str) -> Optional[VerifyResult]:
    """Duration sanity check + full-file silence scan, run right after merge
    and before the original is archived/locked. Catches a broken concat or a
    recording that is mostly dead air, immediately instead of weeks later."""
    if not merged.is_file():
        return None

    duration = probe_duration(merged)
    if duration is None:
        log.warn("[{}] Could not read merged duration for verification.".format(label))
        duration = 0.0

    seg_durations = [d for d in (probe_duration(s) for s in segments) if d is not None]
    expected = sum(seg_durations) if seg_durations else None
    if expected is not None:
        tolerance = max(DURATION_TOLERANCE_MIN_S, expected * DURATION_TOLERANCE_PCT)
        if abs(duration - expected) > tolerance:
            log.warn("[{}] Merged duration {:.1f}s differs from segment sum {:.1f}s "
                     "by more than {:.1f}s -- possible broken concat.".format(
                         label, duration, expected, tolerance))

    vol = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(merged), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    mean_m = MEAN_RE.search(vol.stderr)
    max_m = MAX_RE.search(vol.stderr)
    mean_db = float(mean_m.group(1)) if mean_m else None
    max_db = float(max_m.group(1)) if max_m else None

    sil = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(merged),
         "-af", "silencedetect=noise=-60dB:d=0.5", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    silences: List[Tuple[float, float]] = []
    pending_start: Optional[float] = None
    for line in sil.stderr.splitlines():
        start_m = SILENCE_START_RE.search(line)
        if start_m:
            pending_start = float(start_m.group(1))
            continue
        end_m = SILENCE_END_RE.search(line)
        if end_m and pending_start is not None:
            silences.append((pending_start, float(end_m.group(2))))
            pending_start = None

    total_silence = sum(d for _, d in silences)
    coverage = 100.0 * (1.0 - (total_silence / duration)) if duration > 0 else 0.0
    long_silences = [(s, d) for s, d in silences if d > LONG_SILENCE_S]

    log.info("[{}] Coverage: {:.0f}% of recording has signal above -60dB ({:.0f}s / {:.0f}s). "
             "mean={} max={}".format(label, coverage, max(duration - total_silence, 0.0), duration,
                                      fmt_db(mean_db), fmt_db(max_db)))
    if long_silences:
        log.info("[{}] {} stretch(es) >{:.0f}s below threshold: {}".format(
            label, len(long_silences), LONG_SILENCE_S,
            ", ".join("{:.0f}s starting at {:.0f}s".format(d, s) for s, d in long_silences)))

    if coverage < LOW_COVERAGE_PCT:
        log.warn("[{}] LOW COVERAGE ({:.0f}%) -- this track may be mostly silent or broken. "
                 "Check capture.log before relying on it.".format(label, coverage))
        alert(label, coverage)

    return VerifyResult(True, duration, expected, mean_db, max_db, coverage, long_silences)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def append_manifest(basedir: Path, outdir: Path, path: Path,
                     verify: Optional[VerifyResult], segment_count: int, log: Log) -> None:
    """Append-only, human-readable audit log -- answers 'does this file still
    match what was originally recorded' with one sha256sum, forever. No git,
    no backup daemon, just a flat manifest next to the recordings."""
    manifest_path = basedir / "manifest.jsonl"
    entry = {
        "date": outdir.name,
        "path": str(path),
        "sha256": sha256_file(path),
        "duration_s": round(verify.duration_s, 1) if verify else None,
        "coverage_pct": round(verify.coverage_pct, 1) if verify else None,
        "segments": segment_count,
    }
    with open(manifest_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    log.info("Manifest updated: {} ({})".format(manifest_path, path.name))


def build_mixed_copy(mic_path: Path, sys_path: Optional[Path], out_path: Path, log: Log) -> Optional[Path]:
    """Build a mixed-down copy for transcription only. The originals stay
    untouched and separate; this file lives under derived/ and is never
    treated as a source of truth."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if sys_path is not None and sys_path.is_file():
        cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(mic_path), "-i", str(sys_path),
               "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest[a]",
               "-map", "[a]", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out_path)]
    else:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(mic_path),
               "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_path.is_file():
        log.warn("Could not build mixed transcription copy: {}".format(
            proc.stderr.strip().splitlines()[-1] if proc.stderr else "unknown"))
        return None
    return out_path


def transcribe(cfg: Config, merged: Path, log: Log) -> None:
    if not cfg.transcribe:
        return
    if not cfg.model.is_file():
        log.warn("Model not found at {} — skipping transcription.".format(cfg.model))
        log.warn("Download it: curl -L -o {} "
                 "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin".format(cfg.model))
        return

    if shutil.which("whisper-cli"):
        log.info("Transcribing with whisper-cli (this may take a while)...")
        cmd = ["whisper-cli", "-m", str(cfg.model), "-f", str(merged),
               "-otxt", "-of", str(cfg.outdir / "transcript"), "-np", "-pp"]
        subprocess.run(cmd)
        log.info("Transcript: {}".format(cfg.outdir / "transcript.txt"))
    elif shutil.which("whisper-cpp"):
        log.info("Transcribing with whisper-cpp (this may take a while)...")
        cmd = ["whisper-cpp", "-m", str(cfg.model), "-f", str(merged),
               "--output-format", "md", "--output-dir", str(cfg.outdir)]
        subprocess.run(cmd)
        produced = cfg.outdir / "{}.md".format(merged.stem)
        if produced.is_file():
            produced.rename(cfg.outdir / "transcript.md")
        log.info("Transcript: {}".format(cfg.outdir / "transcript.md"))
    else:
        log.warn("Neither whisper-cli nor whisper-cpp found — skipping transcription.")
        log.warn("Install it: brew install whisper-cpp")


def self_test(cfg: Config, system_cands: List[Candidate], outputs: List[Device], log: Log) -> int:
    log.info("Self-test: outputs = {}".format(
        ", ".join(o.name for o in outputs) if outputs else "(none reported by ffmpeg)"))
    if not system_cands:
        log.warn("No loopback/system input found. Zoom audio will not be captured.")
        log.warn("Install a loopback device (BlackHole) or run Zoom so ZoomAudioDevice appears.")
        return 1
    probe_seconds = max(1.5, cfg.probe_seconds)
    total = probe_seconds * len(system_cands) + 2.0
    tmp = Path(tempfile.mkdtemp(prefix="zoomrec_"))
    tone = tmp / "tone.wav"
    gen = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-f", "lavfi",
         "-i", "sine=frequency=1000:duration={:.1f}".format(total),
         "-ar", "48000", "-ac", "1", str(tone)],
        capture_output=True, text=True)
    if gen.returncode != 0 or not tone.is_file():
        log.error("Could not generate test tone.")
        return 1

    log.info("Playing a 1 kHz tone on the default output for {:.1f}s...".format(total))
    player = subprocess.Popen(["afplay", str(tone)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    captured = False
    for cand in system_cands:
        p = probe_level(cand.device, probe_seconds)
        ok = has_signal(p, -50.0)
        captured = captured or ok
        log.info("  - {:<32} {} -> {}".format(
            cand.device.name, fmt_db(p.max_db) if p.ok else "open failed",
            "CAPTURED" if ok else "no tone"))
    try:
        player.wait(timeout=total + 5)
    except subprocess.TimeoutExpired:
        player.kill()
    shutil.rmtree(tmp, ignore_errors=True)
    if captured:
        log.info("Self-test PASSED: output audio is reaching a capturable loopback.")
        return 0
    log.warn("Self-test FAILED: the test tone was not captured by any loopback input.")
    log.warn("Zoom's speaker route may not include a loopback device. Check Zoom audio settings.")
    return 1


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Robust Zoom + microphone recorder with health checks and failover.")
    parser.add_argument("minutes", nargs="?", type=float, default=None,
                        help="segment length in minutes (default 5)")
    parser.add_argument("--minutes", type=float, dest="minutes_opt", default=None,
                        help="segment length in minutes")
    parser.add_argument("--mic", default=os.environ.get("MIC_AUDIO_DEVICE"),
                        help="force a microphone by name (or MIC_AUDIO_DEVICE)")
    parser.add_argument("--system", default=os.environ.get("ZOOM_AUDIO_DEVICE"),
                        help="force the system/loopback input by name (or ZOOM_AUDIO_DEVICE)")
    parser.add_argument("--no-system", action="store_true", help="record microphone only")
    parser.add_argument("--chunk-seconds", type=float, default=5.0,
                        help="how often to test the active mic (default 5)")
    parser.add_argument("--fail-threshold", type=int, default=3,
                        help="consecutive silent checks before cycling inputs (default 3)")
    parser.add_argument("--cycle-seconds", type=float, default=60.0,
                        help="how often to test every inactive input (default 60)")
    parser.add_argument("--probe-seconds", type=float, default=1.5,
                        help="length of each signal probe (default 1.5)")
    parser.add_argument("--silence-db", type=float, default=-60.0,
                        help="max_volume below this counts as silence (default -60)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="whisper model path")
    parser.add_argument("--basedir", default="~/ZoomRecordings", help="output base directory")
    parser.add_argument("--no-transcribe", action="store_true", help="skip transcription")
    parser.add_argument("--list", action="store_true", help="list audio devices and exit")
    parser.add_argument("--self-test", action="store_true",
                        help="play a tone and verify the output->loopback capture path")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found. Install it: brew install ffmpeg", file=sys.stderr)
        return 1
    if not shutil.which("ffprobe"):
        print("ERROR: ffprobe not found. Install it: brew install ffmpeg", file=sys.stderr)
        return 1

    inputs, outputs = list_devices()

    if args.list:
        print("Audio inputs:")
        for d in inputs:
            print("  [{}] {}{}".format(d.index, d.name, "  (loopback)" if LOOPBACK_RE.search(d.name) else ""))
        print("Audio outputs:")
        for d in outputs:
            print("  [{}] {}".format(d.index, d.name))
        return 0

    minutes = args.minutes_opt if args.minutes_opt is not None else args.minutes
    segment_seconds = int((minutes if minutes else 5) * 60)
    basedir = Path(os.path.expanduser(args.basedir))
    outdir = basedir / datetime.now().strftime("%Y-%m-%d")
    outdir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="zoomrec_"))
    log = Log(outdir / "capture.log")

    cfg = Config(
        segment_seconds=segment_seconds,
        chunk_seconds=args.chunk_seconds,
        fail_threshold=args.fail_threshold,
        cycle_seconds=args.cycle_seconds,
        probe_seconds=args.probe_seconds,
        silence_db=args.silence_db,
        mic_override=args.mic,
        system_override=args.system,
        use_system=not args.no_system,
        transcribe=not args.no_transcribe,
        model=Path(os.path.expanduser(args.model)),
        basedir=basedir,
        outdir=outdir,
        workdir=workdir,
    )

    mic_cands = build_mic_candidates(inputs)
    system_cands = build_system_candidates(inputs)

    if args.self_test:
        code = self_test(cfg, system_cands, outputs, log)
        log.close()
        return code

    log.info("zoom-recorder starting. Output: {}".format(outdir))
    log.info("Inputs detected: {}".format(", ".join(d.name for d in inputs) or "(none)"))
    log.info("Outputs detected: {}".format(", ".join(d.name for d in outputs) or "(none)"))

    if not mic_cands:
        log.error("No microphone inputs found.")
        log.close()
        return 1

    mic = choose_mic(cfg, mic_cands, log)
    if mic is None:
        log.error("Could not select a usable microphone.")
        log.close()
        return 1

    system = None
    if cfg.use_system:
        system = choose_system(cfg, system_cands, log)
        if system is None:
            log.warn("No system/loopback input found; recording microphone only.")
            log.warn("Start Zoom or install BlackHole to capture meeting audio.")

    log.info("Selected mic: {} (priority {}, level {})".format(
        mic.device.name, mic.priority,
        fmt_db(mic.probe.max_db) if mic.probe and mic.probe.ok else "n/a"))
    if system is not None:
        log.info("Selected system input: {}".format(system.device.name))

    rec = Recorder(cfg, log)
    stop = threading.Event()

    def handle_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    rec.start(mic.device, system.device if system else None)
    log.info("Recording. Press Ctrl+C to stop and merge.")

    try:
        monitor(cfg, rec, mic_cands, system_cands, stop, log)
    except Exception as exc:  # noqa: BLE001
        log.error("Monitor loop crashed: {}".format(exc))
    finally:
        log.info("Stopping recording...")
        rec.stop()

        mic_segments = rec.segments_mic()
        sys_segments = rec.segments_sys()

        merged_mic = outdir / "recording_mic.wav"
        mic_ok = merge_segments(mic_segments, merged_mic, workdir, log)
        if not mic_ok:
            log.error("No mic audio was recorded.")
            shutil.rmtree(workdir, ignore_errors=True)
            log.close()
            return 1

        merged_sys: Optional[Path] = None
        if sys_segments:
            candidate_sys = outdir / "recording_sys.wav"
            if merge_segments(sys_segments, candidate_sys, workdir, log):
                merged_sys = candidate_sys
            else:
                log.warn("System track merge failed; continuing with mic-only original.")

        log.info("Verifying recording before archiving...")
        verify_mic = verify_recording(merged_mic, mic_segments, log, "mic")
        verify_sys = verify_recording(merged_sys, sys_segments, log, "system") if merged_sys else None

        # Gap 1: never delete segments -- move them to .segments/, and lock the
        # merged originals read-only so nothing (including a future run of
        # this script) can silently overwrite the only copy.
        os.chmod(merged_mic, 0o444)
        if merged_sys:
            os.chmod(merged_sys, 0o444)

        segments_dest = outdir / ".segments"
        segments_dest.mkdir(parents=True, exist_ok=True)
        for sdir in rec.sessions:
            if sdir.is_dir():
                shutil.move(str(sdir), str(segments_dest / sdir.name))
        shutil.rmtree(workdir, ignore_errors=True)
        log.info("Segments archived (not deleted) under {}".format(segments_dest))

        # Gap 4: append-only checksum manifest.
        append_manifest(basedir, outdir, merged_mic, verify_mic, len(mic_segments), log)
        if merged_sys:
            append_manifest(basedir, outdir, merged_sys, verify_sys, len(sys_segments), log)

        log.info("Saved: {}{} ({} mic segments{})".format(
            merged_mic, " + " + str(merged_sys) if merged_sys else "",
            len(mic_segments),
            ", {} system segments".format(len(sys_segments)) if sys_segments else ""))

        mixed = build_mixed_copy(merged_mic, merged_sys, outdir / "derived" / "recording_mixed.wav", log)
        if mixed is not None:
            transcribe(cfg, mixed, log)

        log.info("Done. Originals are read-only under {}/. Derived copies (safe to overwrite) "
                 "belong in {}/derived/.".format(outdir, outdir))
        log.close()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
