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
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from hud.devices import load_topology, system_advice
except Exception:  # noqa: BLE001 - recorder must run even without the HUD package
    load_topology = None  # type: ignore[assignment]

    def system_advice(topo=None) -> str:  # type: ignore[misc]
        return ""

DEFAULT_MODEL = "~/.cache/whisper-cpp/ggml-base.en.bin"
FLOOR_DB = -91.0
LOW_COVERAGE_PCT = 50.0
DURATION_TOLERANCE_PCT = 0.02
DURATION_TOLERANCE_MIN_S = 2.0
LONG_SILENCE_S = 60.0
# Below this, a merged track is a failed capture, not a quiet recording.
MIN_VALID_RECORDING_S = 2.0

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
    (re.compile(r"blackhole|black\s*hole", re.I), 100),
    (re.compile(r"loopback", re.I), 90),
    (re.compile(r"soundflower", re.I), 80),
    (re.compile(r"aggregate|multi-?output", re.I), 70),
    # ZoomAudioDevice only carries Zoom's own shared audio, not the default
    # output, so it is a poor general-purpose system source.
    (re.compile(r"zoom\s*audio\s*device", re.I), 20),
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
    system_capture: str = "auto"  # resolved: "tap" | "loopback"


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


def probe_level(device: Device, seconds: float, max_wait: Optional[float] = None) -> Probe:
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin",
        "-f", "avfoundation", "-i", ":{}".format(device.name),
        "-t", str(seconds),
        "-af", "volumedetect",
        "-f", "null", "-",
    ]
    if max_wait is None:
        max_wait = seconds + 12
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=max_wait)
    except subprocess.TimeoutExpired:
        # A wedged avfoundation open hangs forever; a busy device errors out
        # in a couple of seconds, so a timeout means the open is stuck.
        return Probe(False, None, None, "open timed out after {:.0f}s".format(max_wait))
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


def build_mic_candidates(inputs: List[Device], topo=None) -> List[Candidate]:
    candidates = []
    for dev in inputs:
        if topo is not None and topo.devices:
            if not topo.looks_like_mic(dev.name):
                continue
        elif LOOPBACK_RE.search(dev.name):
            continue
        candidates.append(Candidate(dev, mic_priority(dev.name)))
    # Prefer the system's default input over the static quality ranking.
    default_input = getattr(topo, "default_input", None) if topo is not None else None
    candidates.sort(key=lambda c: (c.device.name != default_input,
                                   -c.priority, c.device.index))
    return candidates


def build_system_candidates(inputs: List[Device], topo=None) -> List[Candidate]:
    candidates = []
    for dev in inputs:
        if topo is not None and topo.devices:
            is_loop = topo.looks_like_loopback(dev.name)
        else:
            is_loop = bool(LOOPBACK_RE.search(dev.name))
        if is_loop:
            candidates.append(Candidate(dev, system_priority(dev.name)))
    candidates.sort(key=lambda c: (-c.priority, c.device.index))
    return candidates


def find_device(inputs: List[Device], name: str) -> Optional[Device]:
    for dev in inputs:
        if dev.name == name:
            return dev
    return None


def choose_mic(cfg: Config, candidates: List[Candidate], log: Log,
               exclude: Optional[str] = None, topo=None) -> Optional[Candidate]:
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

    default_input = getattr(topo, "default_input", None) if topo is not None else None
    usable.sort(key=lambda c: (not has_signal(c.probe, cfg.silence_db),
                               c.device.name != default_input,
                               -c.priority, c.device.index))
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
        cand = Candidate(dev, system_priority(dev.name),
                         probe_level(dev, cfg.probe_seconds))
        if not has_signal(cand.probe, cfg.silence_db):
            log.warn("Requested system device '{}' opened but has no signal yet.".format(dev.name))
        return cand

    usable = [c for c in candidates if not (exclude and c.device.name == exclude)]
    if not usable:
        _warn_system(log, None)
        return None
    for cand in usable:
        if cand.probe is None:
            cand.probe = probe_level(cand.device, cfg.probe_seconds)

    # Prefer a loopback that is actually carrying audio, but never discard a
    # good one just because the call is quiet at startup.
    with_signal = [c for c in usable if has_signal(c.probe, cfg.silence_db)]
    chosen = with_signal[0] if with_signal else usable[0]
    if not with_signal:
        log.warn("System input '{}' opened but is currently silent; will keep monitoring.".format(
            chosen.device.name))
    _warn_system(log, chosen)
    return chosen


def _warn_system(log: Log, chosen: Optional[Candidate]) -> None:
    if chosen is not None and system_priority(chosen.device.name) <= 20:
        log.warn("'{}' only carries Zoom's own shared audio, not general system output.".format(
            chosen.device.name))
    advice = system_advice()
    if advice:
        log.warn(advice)
        if "Multi-Output" in advice or "Install BlackHole" in advice:
            log.warn("Fix it automatically: ./zoom_record.py --fix-routing "
                     "(or use the menu-bar app: Fix Audio Routing).")


def build_capture_cmd(mic_name: str, system_name: Optional[str], pcm_source,
                      sdir: Path, segment_seconds: int) -> Tuple[List[str], tuple]:
    """Pure ffmpeg command assembly for one recording session.

    Returns (cmd, pass_fds). ``pcm_source`` feeds raw PCM via a pipe fd
    (SystemTap); ``system_name`` selects an avfoundation input device.
    """
    mic_pattern = str(sdir / "seg_%05d_mic.wav")
    sys_pattern = str(sdir / "seg_%05d_sys.wav")
    cmd = [
        "ffmpeg", "-hide_banner", "-thread_queue_size", "1024",
        "-f", "avfoundation", "-i", ":{}".format(mic_name),
    ]
    pass_fds: tuple = ()
    if system_name is not None:
        cmd += [
            "-thread_queue_size", "1024",
            "-f", "avfoundation", "-i", ":{}".format(system_name),
        ]
    elif pcm_source is not None:
        cmd += pcm_source.ffmpeg_args()
        pass_fds = (pcm_source.read_fd,)
    segment_opts = [
        "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
    ]
    cmd += ["-map", "0:a"] + segment_opts + [mic_pattern]
    if system_name is not None or pcm_source is not None:
        cmd += ["-map", "1:a"] + segment_opts + [sys_pattern]
    return cmd, pass_fds


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
        self.pcm_source = None  # SystemTap in tap mode
        self.sessions: List[Path] = []
        self._session = 0
        self._stderr_fh = None
        self.on_restart = None  # optional callback fired after a (re)start

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, mic: Device, system: Optional[Device],
              pcm_source=None) -> None:
        """pcm_source: a SystemTap (or similar) feeding PCM via a pipe fd
        instead of an avfoundation device."""
        self.stop()
        self._session += 1
        sdir = self.cfg.workdir / "session_{:04d}".format(self._session)
        sdir.mkdir(parents=True, exist_ok=True)
        self.sessions.append(sdir)

        cmd, pass_fds = build_capture_cmd(
            mic.name,
            system.name if system is not None else None,
            pcm_source, sdir, int(self.cfg.segment_seconds))

        self._stderr_fh = open(sdir / "ffmpeg.log", "ab")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._stderr_fh,
            pass_fds=pass_fds,
        )
        self.pcm_source = pcm_source
        self.mic = Candidate(mic, mic_priority(mic.name))
        self.system = Candidate(system, 0) if system else None
        src = "{}".format(mic.name)
        if system is not None:
            src += " + {} (separate tracks)".format(system.name)
        elif pcm_source is not None:
            src += " + {} (separate tracks)".format(pcm_source.name())
        self.log.info("Recording session {} -> {}".format(self._session, src))
        if self.on_restart is not None:
            try:
                self.on_restart()
            except Exception:  # noqa: BLE001
                pass

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


def monitor(cfg: Config, rec: Recorder, inputs: List[Device],
            mic_cands: List[Candidate], system_cands: List[Candidate],
            stop: threading.Event, log: Log) -> None:
    silent = 0
    open_failures = 0
    restarted_for_wedge = False
    warned_tap_silent = False
    last_cycle = time.monotonic()
    last_route: Optional[Tuple[Optional[str], Optional[str]]] = None
    tap_mode = cfg.system_capture == "tap"
    while not stop.wait(cfg.chunk_seconds):
        # Track the default input/output: headphones or a Bluetooth switch
        # change the routing mid-call, and the system loopback can drop out.
        # (In tap mode the tap follows the default output automatically, so
        # only the mic needs re-resolving.)
        if load_topology is not None:
            topo = load_topology()
            route = (topo.default_input, topo.default_output)
            if last_route is not None and route != last_route:
                log.warn("Audio route changed: input='{}' output='{}'".format(
                    route[0] or "?", route[1] or "?"))
                mic_cands = build_mic_candidates(inputs, topo) or mic_cands
                if not tap_mode:
                    system_cands = build_system_candidates(inputs, topo) or system_cands
                if cfg.use_system and not tap_mode and route[1] != last_route[1]:
                    new_sys = choose_system(cfg, system_cands, log)
                    cur = rec.system.device.name if rec.system else None
                    if new_sys is not None and new_sys.device.name != cur:
                        log.warn("Switching system input: '{}' -> '{}'".format(
                            cur or "(none)", new_sys.device.name))
                        rec.start(rec.mic.device, new_sys.device,
                                  pcm_source=rec.pcm_source)
                    elif new_sys is None and cur is not None:
                        log.warn("No loopback in the new output path; recording microphone only.")
                        rec.start(rec.mic.device, None, pcm_source=None)
                if route[0] != last_route[0]:
                    new_mic = choose_mic(cfg, mic_cands, log, topo=topo)
                    if new_mic is not None and new_mic.device.name != rec.mic.device.name:
                        log.warn("Switching mic: '{}' -> '{}'".format(
                            rec.mic.device.name, new_mic.device.name))
                        rec.start(new_mic.device, rec.system.device if rec.system else None,
                                  pcm_source=rec.pcm_source)
            last_route = route

        if not rec.is_alive():
            log.error("Recorder exited unexpectedly; restarting.")
            new_mic = choose_mic(cfg, mic_cands, log)
            if new_mic is None:
                log.error("No usable mic; cannot restart.")
                stop.set()
                break
            new_system = rec.system.device if rec.system else None
            rec.start(new_mic.device, new_system, pcm_source=rec.pcm_source)
            silent = 0
            continue

        mic_probe = probe_level(rec.mic.device, cfg.probe_seconds,
                                max_wait=cfg.probe_seconds + 2.5)
        if has_signal(mic_probe, cfg.silence_db):
            if silent:
                log.info("Mic '{}' recovered.".format(rec.mic.device.name))
            silent = 0
            open_failures = 0
            restarted_for_wedge = False
        else:
            silent += 1
            if mic_probe.ok:
                detail = fmt_db(mic_probe.max_db)
            else:
                open_failures += 1
                detail = "probe failed: {}".format(
                    mic_probe.error or "unknown") if mic_probe.error else "open failed"
            log.warn("Mic '{}' silent ({}) — {}/{} before failover.".format(
                rec.mic.device.name, detail, silent, cfg.fail_threshold))

        if silent >= cfg.fail_threshold:
            if open_failures >= cfg.fail_threshold and not restarted_for_wedge:
                # Repeated open failures are NOT silence: the avfoundation
                # open itself is stuck (device wedged by a hung capture).
                # A wedged ffmpeg never recovers, so restart it once.
                log.warn("Mic probes keep failing to open; restarting the capture "
                         "(device may be wedged by the current ffmpeg).")
                log_ffmpeg_tail(rec, log)
                rec.start(rec.mic.device, rec.system.device if rec.system else None,
                          pcm_source=rec.pcm_source)
                silent = 0
                open_failures = 0
                restarted_for_wedge = True
                continue
            log.warn("Mic failed health checks; cycling all inputs for signal...")
            best = hunt_mic(cfg, mic_cands, rec.mic.device.name, log)
            if best is not None:
                log.warn("Switching mic: '{}' -> '{}'".format(rec.mic.device.name, best.name))
                rec.start(best, rec.system.device if rec.system else None,
                          pcm_source=rec.pcm_source)
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
                    rec.start(rec.mic.device, new_sys.device, pcm_source=rec.pcm_source)
                else:
                    log.warn("No replacement system input available; continuing without it.")
                    rec.start(rec.mic.device, None, pcm_source=None)
        elif tap_mode and rec.pcm_source is not None:
            if rec.pcm_source.stalled_after(5.0):
                log.warn("System tap stalled (no data for 5s); it follows the default "
                         "output automatically and should recover on the next sound.")
            if (rec.pcm_source.running_seconds() > 10.0
                    and rec.pcm_source.silent_seconds() > 10.0
                    and not warned_tap_silent):
                log.warn("System tap has delivered only silence so far. If system "
                         "audio should be audible now, grant System Audio Recording "
                         "access: System Settings > Privacy & Security > Screen & "
                         "System Audio Recording.")
                notify_user("System audio tap is capturing silence — grant System "
                            "Audio Recording access in Privacy settings.",
                            title="zoom-recorder: no system audio")
                warned_tap_silent = True
        elif tap_mode and cfg.use_system and rec.pcm_source is None:
            log.warn("No system capture source; recording microphone only.")

        now = time.monotonic()
        if now - last_cycle >= cfg.cycle_seconds:
            last_cycle = now
            if tap_mode and rec.pcm_source is not None:
                log.info("System tap: {}".format(rec.pcm_source.health_line()))
            cycle_probe(cfg, mic_cands, system_cands,
                        rec.mic.device.name if rec.mic else None,
                        rec.system.device.name if rec.system else None, log)


def log_ffmpeg_tail(rec: "Recorder", log: "Log", lines: int = 15) -> None:
    """Surface the running ffmpeg's stderr into capture.log -- on a wedged
    capture this is the only evidence of where it is stuck."""
    try:
        sdir = rec.sessions[-1]
        with open(sdir / "ffmpeg.log", "r", errors="replace") as fh:
            tail = fh.readlines()[-lines:]
    except (OSError, IndexError):
        return
    if tail:
        log.warn("ffmpeg.log tail (session {}):".format(len(rec.sessions)))
        for line in tail:
            log.error("  | {}".format(line.rstrip()))


def preserve_session_diagnostics(rec: "Recorder", outdir: Path, log: "Log") -> None:
    """Keep ffmpeg's stderr and the (possibly still useful) segments when the
    recording is being abandoned: the old failure path rmtree'd the whole
    workdir, destroying the evidence needed to diagnose the failure."""
    dest = outdir / ".segments"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for sdir in rec.sessions:
            if sdir.is_dir():
                shutil.move(str(sdir), str(dest / sdir.name))
    except OSError as exc:
        log.warn("Could not archive failing-session segments: {}".format(exc))


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


def notify_user(message: str, title: str = "zoom-recorder") -> None:
    """macOS notification from the recorder process (capture.log alone is too
    easy to miss when the window is not being watched)."""
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification "{}" with title "{}"'.format(
                 message.replace('"', "'"), title)],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


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

    # A merge of ~zero duration must never read as "100% coverage": a wedged
    # capture that produced only the first frames would look perfect.
    failed_capture = duration < MIN_VALID_RECORDING_S

    log.info("[{}] Coverage: {:.0f}% of recording has signal above -60dB ({:.0f}s / {:.0f}s). "
             "mean={} max={}".format(label, coverage, max(duration - total_silence, 0.0), duration,
                                      fmt_db(mean_db), fmt_db(max_db)))
    if failed_capture:
        log.error("[{}] merged recording is only {:.2f}s -- this was a failed capture, "
                  "not a quiet one.".format(label, duration))
        coverage = 0.0
        alert(label, coverage)
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
        "date": outdir.parent.name,
        "session": outdir.name,
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


def self_test(probe_seconds_arg: float, system_cands: List[Candidate], outputs: List[Device], log: Log) -> int:
    log.info("Self-test: outputs = {}".format(
        ", ".join(o.name for o in outputs) if outputs else "(none reported by ffmpeg)"))
    if not system_cands:
        log.warn("No loopback/system input found. System audio will not be captured.")
        advice = system_advice()
        if advice:
            log.warn(advice)
            log.warn("Fix it automatically: ./zoom_record.py --fix-routing.")
        return 1
    probe_seconds = max(1.5, probe_seconds_arg)
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
    parser.add_argument("--check-routing", action="store_true",
                        help="verify system-audio routing reaches a loopback, then exit")
    parser.add_argument("--fix-routing", action="store_true",
                        help="create the Multi-Output Device (loopback + real output) "
                             "and select it as the default output, then exit")
    parser.add_argument("--fix-output", default=None, metavar="NAME",
                        help="with --fix-routing/--restore-routing: which real output device")
    parser.add_argument("--fix-input", default=None, metavar="NAME",
                        help="with --restore-routing: which microphone to select")
    parser.add_argument("--restore-routing", action="store_true",
                        help="undo everything: real default output/input, remove the "
                             "Multi-Output Device, then exit")
    parser.add_argument("--system-capture", default=os.environ.get("ZOOM_SYSTEM_CAPTURE", "auto"),
                        choices=["auto", "tap", "loopback"],
                        help="how to capture system audio: auto (macOS 14.2+: Core Audio tap; "
                             "loopback otherwise), tap (direct capture, no routing changes, "
                             "volume keys keep working), loopback (BlackHole/Multi-Output)")

    # -- live HUD (opt-in; recording is unchanged when these are not used) ---
    parser.add_argument("--live", action="store_true",
                        help="open the live transcript + AI answer HUD during recording")
    parser.add_argument("--hud-port", type=int, default=None,
                        help="port for the local HUD (default: random free port)")
    parser.add_argument("--no-hud-browser", action="store_true",
                        help="do not auto-open the HUD in a browser")
    parser.add_argument("--live-no-answers", action="store_true",
                        help="live transcript only; never call an answer provider")
    parser.add_argument("--no-live-summary", action="store_true",
                        help="do not generate an end-of-call summary/action items/email")
    parser.add_argument("--live-audio-file", default=None,
                        help="feed a media file to the HUD instead of a live tap (testing)")
    parser.add_argument("--stt-backend", default=None,
                        help="live STT backend: groq | openai | local")
    parser.add_argument("--stt-model", default=None, help="override the STT model")
    parser.add_argument("--stt-chunk-seconds", type=float, default=None,
                        help="live STT chunk length in seconds (default 10)")
    parser.add_argument("--glossary-term", action="append", default=None,
                        help="name/acronym to bias live transcription (repeatable)")
    parser.add_argument("--answer-backend", default=None,
                        help="answer provider: groq | openrouter | openai | ollama")
    parser.add_argument("--answer-model", default=None, help="override the question answer model")
    parser.add_argument("--answer-rolling-model", default=None,
                        help="override the rolling talking-points model")
    parser.add_argument("--answer-interval", type=float, default=None,
                        help="seconds between rolling talking-point refreshes (default 35)")
    parser.add_argument("--kb-dir", action="append", default=None,
                        help="directory of .md files for context (repeatable)")
    parser.add_argument("--kb-top-k", type=int, default=None,
                        help="number of knowledge-base snippets per answer (default 5)")
    parser.add_argument("--kb-embed-backend", default=None,
                        help="KB embeddings: auto | sentence-transformers | ollama | openai")
    parser.add_argument("--kb-reindex", action="store_true",
                        help="rebuild the knowledge-base embedding index")
    parser.add_argument("--self-name", default=None,
                        help="name to label your microphone audio with (default 'You')")
    parser.add_argument("--remote-name", default=None,
                        help="name to label the system/loopback audio with (default 'Others')")
    parser.add_argument("--no-speaker-labels", action="store_true",
                        help="mix mic+system into one unlabelled stream")
    return parser.parse_args(argv)


def build_hud_config(args: argparse.Namespace):
    """Load HUD settings from disk (if any) and apply CLI overrides.

    Imported lazily so the recorder keeps working with no HUD dependencies
    installed and unchanged behaviour when --live is not passed.
    """
    from hud.config import load_config

    cfg = load_config()
    cfg.enabled = True
    if args.hud_port is not None:
        cfg.port = args.hud_port
    if args.no_hud_browser:
        cfg.open_browser = False
    if args.live_no_answers:
        cfg.answers_enabled = False
    if args.no_live_summary:
        cfg.summary_enabled = False
    if args.live_audio_file:
        cfg.audio_file = args.live_audio_file
    if args.stt_backend:
        cfg.stt_backend = args.stt_backend
    if args.stt_model:
        cfg.stt_model = args.stt_model
    if args.stt_chunk_seconds is not None:
        cfg.stt_chunk_seconds = args.stt_chunk_seconds
    if args.glossary_term:
        cfg.stt_glossary = args.glossary_term
    if args.answer_backend:
        cfg.answers_backend = args.answer_backend
    if args.answer_model:
        cfg.chat_model = args.answer_model
    if args.answer_rolling_model:
        cfg.rolling_model = args.answer_rolling_model
    if args.answer_interval is not None:
        cfg.answer_interval = args.answer_interval
    if args.kb_dir:
        cfg.kb_dirs = args.kb_dir
    if args.kb_top_k is not None:
        cfg.kb_top_k = args.kb_top_k
    if args.kb_embed_backend:
        cfg.kb_embed_backend = args.kb_embed_backend
    if args.kb_reindex:
        cfg.kb_reindex = True
    if args.self_name:
        cfg.self_name = args.self_name
    if args.remote_name:
        cfg.remote_name = args.remote_name
    if args.no_speaker_labels:
        cfg.speakers_enabled = False
    return cfg


def in_granted_terminal() -> bool:
    """True when the recorder runs under Apple Terminal.

    macOS attributes audio-capture permission to the responsible app: a
    process spawned from Terminal inherits Terminal's grants, while a
    launchd/GUI-spawned process (menu bar) is attributed to its own context,
    where the System Audio Recording grant cannot be created on macOS 15
    (the tap starts but delivers silence, and wedges mic opens)."""
    return os.environ.get("TERM_PROGRAM") == "Apple_Terminal"


def resolve_system_capture(args: argparse.Namespace) -> str:
    """Decide how system audio is captured: tap or loopback."""
    if args.system_capture == "loopback" or args.system:
        return "loopback"
    if args.system_capture == "tap":
        return "tap"
    if not args.no_system and in_granted_terminal():
        try:
            from hud.system_tap import available
            if available():
                return "tap"
        except Exception:  # noqa: BLE001
            pass
    return "loopback"


def run_routing_fix(args: argparse.Namespace) -> int:
    try:
        from hud.routing_fix import fix_routing, restore_routing
    except Exception as exc:  # noqa: BLE001
        print("ERROR: routing fix unavailable: {}".format(exc), file=sys.stderr)
        return 1
    topo = load_topology(force=True) if load_topology is not None else None
    if args.restore_routing:
        result = restore_routing(topo, output=args.fix_output, input_device=args.fix_input)
    else:
        result = fix_routing(topo, physical_output=args.fix_output)
    print(result.message)
    if result.ok and result.changed:
        print("Verify the capture path: ./zoom_record.py --self-test")
    return 0 if result.ok else 1


def fallback_tap_to_loopback(cfg: Config, rec: "Recorder", log: "Log",
                             use_system: bool) -> Tuple[List[Candidate], List[Device]]:
    """Abandon tap capture for this session and continue with the proven
    loopback path. Returns (system_candidates, inputs) for the monitor loop."""
    if rec.pcm_source is not None:
        try:
            rec.pcm_source.stop()
        except Exception:  # noqa: BLE001
            pass
        rec.pcm_source = None
    cfg.system_capture = "loopback"
    log.warn("Tap capture abandoned for this session; switching to loopback mode.")
    try:
        from hud.routing_fix import fix_routing
        result = fix_routing(topo=None, assume_yes=True)
        first = result.message.splitlines()[0] if result.message else ""
        log.warn("Loopback routing: {}".format(first or ("OK" if result.ok else "failed")))
        if not result.ok:
            log.warn(result.message)
    except Exception as exc:  # noqa: BLE001
        log.warn("Loopback routing setup failed ({}); continuing without system audio.".format(exc))
    topo = load_topology(force=True) if load_topology is not None else None
    inputs, _outputs = list_devices()
    sys_cands = build_system_candidates(inputs, topo)
    if use_system:
        system = choose_system(cfg, sys_cands, log)
        if system is None:
            log.warn("No loopback input available after fallback; recording microphone only.")
            rec.start(rec.mic.device, None)
        else:
            rec.start(rec.mic.device, system.device)
    else:
        rec.start(rec.mic.device, None)
    return sys_cands, inputs


def wait_for_first_segment(rec: "Recorder", timeout: float = 6.0) -> bool:
    """True once ffmpeg has written its first mic segment (capture actually
    flowing). A hung avfoundation open produces a live-but-silent ffmpeg that
    would otherwise go unnoticed until the end of the meeting."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if rec.segments_mic():
            return True
        if rec.proc is None or rec.proc.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    if args.fix_routing or args.restore_routing:
        return run_routing_fix(args)
    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found. Install it: brew install ffmpeg", file=sys.stderr)
        return 1
    if not shutil.which("ffprobe"):
        print("ERROR: ffprobe not found. Install it: brew install ffmpeg", file=sys.stderr)
        return 1

    inputs, outputs = list_devices()
    topo = load_topology(force=True) if load_topology is not None else None
    system_capture = resolve_system_capture(args)

    if args.list:
        def _annotate(name: str) -> str:
            dev = topo.device(name) if topo is not None else None
            tags = []
            if dev is not None and dev.is_loopback:
                tags.append("loopback")
            if dev is not None and dev.default_input:
                tags.append("default input")
            if dev is not None and dev.default_output:
                tags.append("default output")
            if topo is not None and not topo.devices and LOOPBACK_RE.search(name):
                tags.append("loopback")
            return "  ({})".format(", ".join(tags)) if tags else ""

        print("Audio inputs:")
        for d in inputs:
            print("  [{}] {}{}".format(d.index, d.name, _annotate(d.name)))
        print("Audio outputs:")
        for d in outputs:
            print("  [{}] {}{}".format(d.index, d.name, _annotate(d.name)))
        if topo is not None:
            print("Default input:  {}".format(topo.default_input or "(unknown)"))
            print("Default output: {}".format(topo.default_output or "(unknown)"))
        print("System capture: {}".format(
            "Core Audio tap (direct, no routing changes)" if system_capture == "tap"
            else "loopback device (Multi-Output route)"))
        advice = system_advice(topo)
        if advice and system_capture == "loopback":
            print("System audio:   {}".format(advice))
            print("                Fix automatically: ./zoom_record.py --fix-routing")
        return 0

    minutes = args.minutes_opt if args.minutes_opt is not None else args.minutes
    segment_seconds = int((minutes if minutes else 5) * 60)
    basedir = Path(os.path.expanduser(args.basedir))

    mic_cands = build_mic_candidates(inputs, topo)
    system_cands = build_system_candidates(inputs, topo)

    if args.self_test or args.check_routing:
        # No recording happens, so no dated/session folder is created for it --
        # just log to the console.
        if system_capture == "tap":
            from hud.system_tap import system_tap_self_test
            code = 0 if system_tap_self_test(args.probe_seconds, log=Log(None).info) else 1
            return code
        code = self_test(args.probe_seconds, system_cands, outputs, Log(None))
        return code

    # Every recording gets its own folder: <basedir>/<day>/<HH-MM-SS>_<uid>/.
    # A same-second collision is already effectively impossible; if it ever
    # happens anyway, just draw a fresh UID rather than failing the recording.
    started = datetime.now()
    day_dir = basedir / started.strftime("%Y-%m-%d")
    outdir = None
    for _ in range(5):
        session_name = "{}_{}".format(started.strftime("%H-%M-%S"), uuid.uuid4().hex[:8])
        candidate = day_dir / session_name
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        outdir = candidate
        break
    if outdir is None:
        print("ERROR: could not find an unused session folder under {} after "
              "several tries.".format(day_dir), file=sys.stderr)
        return 1
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
        system_capture=system_capture,
    )

    log.info("zoom-recorder starting. Output: {}".format(outdir))
    log.info("Inputs detected: {}".format(", ".join(d.name for d in inputs) or "(none)"))
    log.info("Outputs detected: {}".format(", ".join(d.name for d in outputs) or "(none)"))

    if not mic_cands:
        log.error("No microphone inputs found.")
        log.close()
        return 1

    mic = choose_mic(cfg, mic_cands, log, topo=topo)
    if mic is None:
        log.error("Could not select a usable microphone.")
        log.close()
        return 1

    system = None
    pcm_source = None
    if cfg.use_system:
        if cfg.system_capture == "tap":
            try:
                from hud.system_tap import SystemTap
                pcm_source = SystemTap(log=log.info)
                # Start the tap's IO BEFORE ffmpeg opens the mic: starting
                # tap IO while an avfoundation mic open is in flight can
                # wedge the open (macOS 15), and starting it afterwards can
                # kill the running mic stream. Settled-first avoids both.
                pcm_source.start()
                time.sleep(0.5)
                log.info("System audio: macOS process tap (output routing untouched; "
                         "volume keys keep working and recording level does not "
                         "follow the volume slider).")
            except Exception as exc:  # noqa: BLE001
                log.warn("System tap unavailable ({}); falling back to loopback capture.".format(exc))
                pcm_source = None
                cfg.system_capture = "loopback"
        if pcm_source is None:
            # Loopback capture needs the multi-output route in place; set it
            # up (idempotent) unless the user pinned a specific device.
            if not args.system and load_topology is not None:
                try:
                    from hud.routing_fix import fix_routing
                    result = fix_routing(topo, assume_yes=True)
                    if result.changed:
                        log.info("Routing: {}".format(result.message.splitlines()[0]))
                        topo = load_topology(force=True)
                except Exception as exc:  # noqa: BLE001
                    log.warn("Loopback routing setup skipped ({})".format(exc))
                if topo is not None:
                    advice = system_advice(topo)
                    if advice:
                        log.warn(advice)
            system = choose_system(cfg, system_cands, log)
            if system is None:
                log.warn("No usable system/loopback input; recording microphone only.")
                advice = system_advice(topo)
                if advice:
                    log.warn(advice)

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

    rec = Recorder(cfg, log)
    stop = threading.Event()

    def handle_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    def system_name():
        if system is not None:
            return system.device.name
        if pcm_source is not None:
            return pcm_source.name()
        return None

    # Start the HUD first so the browser URL is ready in ~2s; the transcript
    # begins flowing as soon as the capture produces segments.
    live = None
    if args.live:
        try:
            from hud.session import LiveSession

            hud_cfg = build_hud_config(args)
            live = LiveSession(
                hud_cfg, outdir, log.info,
                mic.device.name,
                system_name(),
                cfg.model,
            )
            live.start()
            rec.on_restart = lambda: live.update_devices(
                rec.mic.device.name if rec.mic else None,
                system_name())
        except Exception as exc:  # noqa: BLE001
            log.warn("Live HUD unavailable ({}); recording continues normally.".format(exc))
            live = None

    rec.start(mic.device, system.device if system else None, pcm_source=pcm_source)
    healthy = wait_for_first_segment(rec)
    if not healthy:
        log.warn("No mic audio 6s after start; ffmpeg looks stuck. Restarting once.")
        log_ffmpeg_tail(rec, log)
        rec.start(mic.device, system.device if system else None, pcm_source=pcm_source)
        healthy = wait_for_first_segment(rec)
    if not healthy:
        log_ffmpeg_tail(rec, log)
        if pcm_source is not None:
            # Tap mode wedges the mic open in some process contexts
            # (launchd/GUI-spawned python on macOS 15). Continue the
            # recording on the proven loopback path instead of failing.
            log.warn("Mic capture wedged even after a restart; falling back "
                     "to loopback capture.")
            system_cands, inputs = fallback_tap_to_loopback(cfg, rec, log, cfg.use_system)
            system = None
            healthy = wait_for_first_segment(rec)
    if not healthy:
        log.warn("Mic capture still not producing data; monitoring will keep "
                 "watching it.")
    log.info("Recording. Press Ctrl+C to stop and merge.")

    try:
        monitor(cfg, rec, inputs, mic_cands, system_cands, stop, log)
    except Exception as exc:  # noqa: BLE001
        log.error("Monitor loop crashed: {}".format(exc))
    finally:
        log.info("Stopping recording...")
        rec.stop()
        if pcm_source is not None:
            try:
                pcm_source.stop()
            except Exception as exc:  # noqa: BLE001
                log.warn("System tap shutdown error: {}".format(exc))
        if live is not None:
            try:
                live.stop()
            except Exception as exc:  # noqa: BLE001
                log.warn("Live HUD shutdown error: {}".format(exc))

        mic_segments = rec.segments_mic()
        sys_segments = rec.segments_sys()

        merged_mic = outdir / "recording_mic.wav"
        mic_ok = merge_segments(mic_segments, merged_mic, workdir, log)
        mic_duration = probe_duration(merged_mic) if mic_ok else None
        if not mic_ok or (mic_duration is not None
                          and mic_duration < MIN_VALID_RECORDING_S):
            log.error("No usable mic audio was recorded{}.".format(
                " (merged track is only {:.2f}s)".format(mic_duration)
                if mic_duration is not None else ""))
            log_ffmpeg_tail(rec, log)
            preserve_session_diagnostics(rec, outdir, log)
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
