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
    from hud.integrity import build_report, source_record, write_report
except Exception:  # noqa: BLE001 - recorder must run even without the HUD package
    load_topology = None  # type: ignore[assignment]

    def system_advice(topo=None) -> str:  # type: ignore[misc]
        return ""

    def build_report(**kwargs):  # type: ignore[misc]
        return kwargs

    def source_record(**kwargs):  # type: ignore[misc]
        return kwargs

    def write_report(path: Path, report) -> None:  # type: ignore[misc]
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

DEFAULT_MODEL = "~/.cache/whisper-cpp/ggml-large-v3-turbo-q5_0.bin"
STOP_REQUEST_FILE = Path.home() / ".zoom_recorder_stop_requested"
FLOOR_DB = -91.0
TAP_STARTUP_GRACE_SECONDS = 30.0
TAP_SILENCE_CONFIRM_SECONDS = 10.0
LOW_COVERAGE_PCT = 50.0
DURATION_TOLERANCE_PCT = 0.02
DURATION_TOLERANCE_MIN_S = 2.0
LONG_SILENCE_S = 60.0
# Below this, a merged track is a failed capture, not a quiet recording.
MIN_VALID_RECORDING_S = 2.0

# Desktop notifications (--no-notifications / --offline disable them).
_NOTIFICATIONS = True


def set_notifications(enabled: bool) -> None:
    global _NOTIFICATIONS
    _NOTIFICATIONS = bool(enabled)

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
    record_mic: bool = True       # False for system-only recordings


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


def build_capture_cmd(mic_name: Optional[str], system_name: Optional[str], pcm_source,
                      sdir: Path, segment_seconds: int) -> Tuple[List[str], tuple]:
    """Pure ffmpeg command assembly for one recording session.

    Any of the two sources may be absent (microphone-only or system-only
    recordings). Returns (cmd, pass_fds); ``pcm_source`` feeds raw PCM via a
    pipe fd (SystemTap), ``system_name`` selects an avfoundation input device.
    """
    mic_pattern = str(sdir / "seg_%05d_mic.wav")
    sys_pattern = str(sdir / "seg_%05d_sys.wav")
    cmd = ["ffmpeg", "-hide_banner"]
    pass_fds: tuple = ()
    mic_index: Optional[int] = None
    sys_index: Optional[int] = None
    index = 0
    if mic_name is not None:
        cmd += ["-thread_queue_size", "1024",
                "-f", "avfoundation", "-i", ":{}".format(mic_name)]
        mic_index = index
        index += 1
    if system_name is not None:
        cmd += ["-thread_queue_size", "1024",
                "-f", "avfoundation", "-i", ":{}".format(system_name)]
        sys_index = index
        index += 1
    elif pcm_source is not None:
        cmd += pcm_source.ffmpeg_args()
        pass_fds = (pcm_source.read_fd,)
        sys_index = index
        index += 1
    segment_opts = [
        "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
    ]
    if mic_index is not None:
        cmd += ["-map", "{}:a".format(mic_index)] + segment_opts + [mic_pattern]
    if sys_index is not None:
        cmd += ["-map", "{}:a".format(sys_index)] + segment_opts + [sys_pattern]
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
        self.on_system_warning = None  # optional live-HUD warning callback
        self.on_integrity_update = None  # optional durable source-health callback
        self.on_route_update = None  # optional durable routing callback
        self.source_states = {}
        self.route_context = {}

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, mic: Optional[Device], system: Optional[Device],
              pcm_source=None) -> None:
        """Either source may be None (microphone-only / system-only).
        pcm_source: a SystemTap (or similar) feeding PCM via a pipe fd
        instead of an avfoundation device."""
        self.stop()
        self._session += 1
        sdir = self.cfg.workdir / "session_{:04d}".format(self._session)
        sdir.mkdir(parents=True, exist_ok=True)
        self.sessions.append(sdir)

        cmd, pass_fds = build_capture_cmd(
            mic.name if mic is not None else None,
            system.name if system is not None else None,
            pcm_source, sdir, int(self.cfg.segment_seconds))

        self._stderr_fh = open(sdir / "ffmpeg.log", "ab")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._stderr_fh,
            pass_fds=pass_fds,
        )
        self.pcm_source = pcm_source
        self.mic = Candidate(mic, mic_priority(mic.name)) if mic else None
        self.system = Candidate(system, 0) if system else None
        src = mic.name if mic is not None else "(no microphone)"
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

    def rebind_outdir(self, old_outdir: Path, new_outdir: Path) -> None:
        """Rebase segment paths after the HUD adds a readable session slug."""
        old_root = Path(old_outdir)
        new_root = Path(new_outdir)
        if old_root == new_root:
            return
        rebound: List[Path] = []
        for session in self.sessions:
            try:
                rebound.append(new_root / session.relative_to(old_root))
            except ValueError:
                rebound.append(session)
        self.sessions = rebound
        self.cfg.outdir = new_root
        self.cfg.workdir = new_root / ".work"

    def reconcile_sessions(self, outdir: Path) -> None:
        """Rebuild in-memory segment directories from the live work tree.

        The recorder can survive a HUD rename, a restart, or a partial shutdown
        only if disk is authoritative. Existing paths are retained when valid,
        then any session directory containing raw WAV segments is added once.
        """
        root = Path(outdir) / ".work"
        discovered = []
        if root.is_dir():
            discovered = sorted(
                (path for path in root.glob("session_*") if path.is_dir()),
                key=lambda path: path.name,
            )
        merged = []
        seen = set()
        for path in list(self.sessions) + discovered:
            resolved = str(path)
            if resolved not in seen and path.is_dir():
                seen.add(resolved)
                merged.append(path)
        self.sessions = merged

    def set_system_warning(self, message: str) -> None:
        """Publish loopback readiness without coupling Recorder to the HUD."""
        callback = self.on_system_warning
        if callback is not None:
            try:
                callback(str(message or ""))
            except Exception:
                pass

    def set_route_context(self, context: Optional[dict]) -> None:
        """Publish the latest routing evidence without coupling Recorder to the HUD."""
        update = dict(context or {})
        if update == self.route_context:
            return
        self.route_context = update
        callback = self.on_route_update
        if callback is not None:
            try:
                callback(dict(update))
            except Exception:
                pass

    def set_source_state(self, source: str, state: str, reason: str = "",
                         device: Optional[str] = None) -> None:
        """Publish a source transition only when its evidence changes."""
        current = self.source_states.get(source, {})
        update = {"state": state, "reason": reason or None,
                  "selected_device": device or current.get("selected_device")}
        if current == update:
            return
        self.source_states[source] = update
        callback = self.on_integrity_update
        if callback is not None:
            try:
                callback(source, state, reason, update["selected_device"])
            except Exception:
                pass


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
    warned_tap_silent = False
    system_silent = 0
    system_recovery_attempted = False
    system_alerted = False
    last_cycle = time.monotonic()
    last_route: Optional[Tuple[Optional[str], Optional[str]]] = None
    tap_mode = cfg.system_capture == "tap"
    while not stop.wait(cfg.chunk_seconds):
        mic_device = rec.mic.device if rec.mic else None
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
                        rec.start(mic_device, new_sys.device,
                                  pcm_source=rec.pcm_source)
                    elif new_sys is None and cur is not None:
                        log.warn("No loopback in the new output path; recording microphone only.")
                        rec.start(mic_device, None, pcm_source=None)
                if mic_device is not None and route[0] != last_route[0]:
                    new_mic = choose_mic(cfg, mic_cands, log, topo=topo)
                    if new_mic is not None and new_mic.device.name != mic_device.name:
                        log.warn("Switching mic: '{}' -> '{}'".format(
                            mic_device.name, new_mic.device.name))
                        rec.start(new_mic.device, rec.system.device if rec.system else None,
                                  pcm_source=rec.pcm_source)
                try:
                    from hud.routing_fix import route_snapshot
                    rec.set_route_context(route_snapshot(
                        topo, rec.system.device.name if rec.system else None,
                        "core_audio_tap" if tap_mode else "loopback"))
                except Exception:
                    pass
            last_route = route

        if not rec.is_alive():
            log.error("Recorder exited unexpectedly; restarting.")
            if cfg.record_mic:
                rec.set_source_state("mic", "failed", "capture process exited",
                                     mic_device.name if mic_device else None)
            if cfg.use_system:
                rec.set_source_state("system", "failed", "capture process exited",
                                     rec.system.device.name if rec.system else None)
            new_system = rec.system.device if rec.system else None
            if mic_device is None:
                rec.start(None, new_system, pcm_source=rec.pcm_source)
                rec.set_source_state("system", "awaiting_signal", "capture process restarted",
                                     new_system.name if new_system else None)
                silent = 0
                continue
            new_mic = choose_mic(cfg, mic_cands, log)
            if new_mic is None:
                log.error("No usable mic; cannot restart.")
                stop.set()
                break
            rec.start(new_mic.device, new_system, pcm_source=rec.pcm_source)
            rec.set_source_state("mic", "awaiting_signal", "capture process restarted",
                                 new_mic.device.name)
            if cfg.use_system:
                rec.set_source_state("system", "awaiting_signal", "capture process restarted",
                                     new_system.name if new_system else None)
            silent = 0
            continue

        if mic_device is None:
            # System-only recording: no microphone health checks to run.
            if rec.system is not None:
                sys_probe = probe_level(rec.system.device, cfg.probe_seconds)
                if not sys_probe.ok:
                    log.warn("System input '{}' failed to open; cycling...".format(
                        rec.system.device.name))
                    new_sys = choose_system(cfg, system_cands, log,
                                            exclude=rec.system.device.name)
                    if new_sys is not None:
                        rec.start(None, new_sys.device, pcm_source=rec.pcm_source)
                        rec.set_source_state("system", "awaiting_signal",
                                             "switched after system input open failure",
                                             new_sys.device.name)
                    else:
                        rec.set_source_state("system", "unavailable",
                                             "system input failed to open and no replacement exists")
            elif tap_mode and rec.pcm_source is not None:
                if rec.pcm_source.stalled_after(5.0):
                    log.warn("System tap stalled (no data for 5s).")
        else:
            # Do not open a second avfoundation reader against the active mic.
            # On macOS that can block the real recorder, create false silence,
            # and trigger the very restart loop meant to recover it. Process
            # liveness plus the startup signal check are safer evidence for the
            # active source; inactive candidates may still be probed below.
            if silent:
                log.info("Mic capture process remains live (active mic probe skipped).")
            silent = 0

        # The system-only branch above already probes this source. Avoid a
        # second reader in the same interval; repeated avfoundation opens can
        # themselves destabilize an otherwise healthy capture.
        if rec.system is not None and mic_device is not None:
            sys_probe = probe_level(rec.system.device, cfg.probe_seconds)
            if not sys_probe.ok:
                rec.set_source_state("system", "unavailable",
                                     "system input failed to open",
                                     rec.system.device.name)
                log.warn("System input '{}' failed to open; cycling...".format(rec.system.device.name))
                new_sys = choose_system(cfg, system_cands, log, exclude=rec.system.device.name)
                if new_sys is not None:
                    log.warn("Switching system input: '{}' -> '{}'".format(
                        rec.system.device.name, new_sys.device.name))
                    rec.start(mic_device, new_sys.device, pcm_source=rec.pcm_source)
                    rec.set_source_state("system", "awaiting_signal",
                                         "switched after system input open failure",
                                         new_sys.device.name)
                else:
                    log.warn("No replacement system input available; continuing without it.")
                    rec.start(mic_device, None, pcm_source=None)
                    rec.set_source_state("system", "unavailable",
                                         "no replacement system input available")
                system_silent = 0
            elif not has_signal(sys_probe, cfg.silence_db):
                # Opening a loopback successfully is not enough: BlackHole
                # can be a perfectly healthy device that is receiving no
                # output. Only escalate while a microphone source is selected
                # and the recorder is live; the bounded threshold avoids
                # alarming during a genuinely quiet call.
                if mic_device is not None:
                    system_silent += 1
                    log.warn("System input '{}' carries no live signal while the mic is active "
                             "({}/{} checks).".format(
                                 rec.system.device.name, system_silent,
                                 max(2, cfg.fail_threshold)))
                else:
                    system_silent = 0
                rec.set_source_state(
                    "system",
                    "silent" if system_silent >= max(2, cfg.fail_threshold) else "awaiting_signal",
                    "no live signal observed",
                    rec.system.device.name,
                )
                if system_silent >= max(2, cfg.fail_threshold) and not system_recovery_attempted:
                    system_recovery_attempted = True
                    warning = ("CRITICAL: Other-party audio has not been confirmed. The "
                               "microphone is active, but the selected loopback is silent. "
                               "Set the call app's speaker to zoom-recorder Multi-Output, "
                               "then retry the route.")
                    rec.set_system_warning(warning)
                    log.warn(warning)
                    recovered = False
                    if cfg.system_capture == "loopback" and not cfg.system_override:
                        try:
                            from hud.routing_fix import fix_routing
                            result = fix_routing(load_topology(force=True) if load_topology else None,
                                                 assume_yes=True)
                            log.info("Audio self-check route retry: {}".format(
                                result.message.splitlines()[0] if result.message else result.ok))
                        except Exception as exc:  # noqa: BLE001 - recovery is best effort
                            log.warn("Audio self-check route retry failed: {}".format(exc))
                        refreshed = load_topology(force=True) if load_topology else None
                        retry_cands = build_system_candidates(inputs, refreshed)
                        new_sys = choose_system(cfg, retry_cands, log)
                        if new_sys is not None and has_signal(new_sys.probe, cfg.silence_db):
                            rec.start(mic_device, new_sys.device, pcm_source=rec.pcm_source)
                            system_recovery_attempted = False
                            system_silent = 0
                            system_alerted = False
                            rec.set_system_warning("")
                            rec.set_route_context({**rec.route_context,
                                                   "application_route_verified": True,
                                                   "signal_verified_device": new_sys.device.name})
                            rec.set_source_state("system", "captured",
                                                 "route recovery signal confirmed",
                                                 new_sys.device.name)
                            log.info("Audio self-check recovered system input '{}'.".format(
                                new_sys.device.name))
                            continue
                    if not system_alerted:
                        system_alerted = True
                        critical_alert(
                            "Other-party audio has not been captured. The selected system "
                            "audio route is silent while your microphone is active. Open "
                            "Audio MIDI Setup or the meeting app's audio settings, select "
                            "the call app's speaker to the managed Multi-Output device, "
                            "then retry the route.",
                            title="zoom-recorder: audio capture problem")
            else:
                if system_silent or system_recovery_attempted:
                    log.info("System input '{}' has live signal again.".format(
                        rec.system.device.name))
                system_silent = 0
                system_recovery_attempted = False
                system_alerted = False
                rec.set_system_warning("")
                rec.set_route_context({**rec.route_context,
                                       "application_route_verified": True,
                                       "signal_verified_device": rec.system.device.name})
                rec.set_source_state("system", "captured", "live signal confirmed",
                                     rec.system.device.name)
        elif tap_mode and rec.pcm_source is not None:
            if rec.pcm_source.stalled_after(5.0):
                log.warn("System tap stalled (no data for 5s); it follows the default "
                         "output automatically and should recover on the next sound.")
            tap_running = rec.pcm_source.running_seconds()
            tap_silent = rec.pcm_source.silent_seconds()
            # A process tap can need several seconds to attach to the current
            # output graph. Do not call that normal initialization a failed
            # route: a meeting may also begin quietly before the first remote
            # utterance. Once the grace period is over, a continuously silent
            # tap is still escalated promptly.
            if (tap_running > TAP_STARTUP_GRACE_SECONDS
                    and tap_silent > TAP_SILENCE_CONFIRM_SECONDS
                    and not warned_tap_silent):
                log.warn("System tap has delivered only silence so far. If system "
                         "audio should be audible now, grant System Audio Recording "
                         "access: System Settings > Privacy & Security > Screen & "
                         "System Audio Recording.")
                notify_user("System audio tap is capturing silence — grant System "
                            "Audio Recording access in Privacy settings.",
                            title="zoom-recorder: no system audio")
                rec.set_system_warning(
                    "Other-party audio has not been confirmed. The macOS system-audio tap "
                    "is delivering silence; grant Screen & System Audio Recording access.")
                rec.set_source_state("system", "silent", "system tap delivered silence",
                                     rec.pcm_source.name())
                critical_alert(
                    "The macOS system-audio tap is silent. Other-party audio is not being "
                    "captured; grant Screen & System Audio Recording access, then restart "
                    "the recording.", title="zoom-recorder: audio capture problem")
                warned_tap_silent = True
            elif tap_running > TAP_STARTUP_GRACE_SECONDS:
                rec.set_source_state("system", "captured", "system tap is producing data",
                                     rec.pcm_source.name())
            else:
                rec.set_source_state("system", "awaiting_signal",
                                     "waiting for initial system audio",
                                     rec.pcm_source.name())
        elif tap_mode and cfg.use_system and rec.pcm_source is None:
            log.warn("No system capture source; recording microphone only.")
            rec.set_source_state("system", "unavailable", "system tap is not running")

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
    workdir.mkdir(parents=True, exist_ok=True)
    # Keep a real media suffix on the temporary path.  FFmpeg infers the
    # output muxer from the filename; a suffix such as ``.partial`` makes it
    # reject an otherwise valid WAV merge with ``Invalid argument``.  The
    # leading dot still keeps the incomplete artifact out of the canonical
    # recording list until os.replace() publishes it atomically.
    temporary = out_path.with_name(".{}.partial.wav".format(out_path.stem))
    try:
        temporary.unlink(missing_ok=True)
        if len(segments) == 1:
            shutil.copy2(segments[0], temporary)
        else:
            concat_list = workdir / "concat_{}.txt".format(out_path.stem)
            with open(concat_list, "w", encoding="utf-8") as fh:
                for seg in segments:
                    fh.write("file '{}'\n".format(seg))
                fh.flush()
                os.fsync(fh.fileno())
            cmd = ["ffmpeg", "-y", "-hide_banner", "-f", "concat", "-safe", "0",
                   "-i", str(concat_list), "-c", "copy", str(temporary)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                log.error("Merge failed: {}".format(
                    proc.stderr.strip().splitlines()[-1] if proc.stderr else "unknown"))
                return False
        if not temporary.is_file() or temporary.stat().st_size <= 44:
            log.error("Merge failed: temporary output is missing or empty.")
            return False
        os.replace(temporary, out_path)
        return True
    except OSError as exc:
        log.error("Merge failed: {}".format(exc))
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def raw_segment_paths(outdir: Path, suffix: str) -> List[Path]:
    """Find raw segments from disk, including sessions archived after a crash."""
    roots = [Path(outdir) / ".work", Path(outdir) / ".segments"]
    found = set()
    for root in roots:
        if not root.is_dir():
            continue
        for session_dir in sorted(root.glob("session_*"), key=lambda path: path.name):
            if not session_dir.is_dir():
                continue
            for segment in sorted(session_dir.glob("seg_*_{}.wav".format(suffix))):
                found.add(segment)
    return sorted(found, key=lambda path: (path.parent.name, path.name, str(path)))


def _load_integrity_expectations(outdir: Path) -> Tuple[bool, bool, dict]:
    path = Path(outdir) / "recording_integrity.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = payload.get("expected_sources", {})
        sources = payload.get("sources", {})
        return bool(expected.get("mic")), bool(expected.get("system")), sources
    except (OSError, ValueError, TypeError):
        return False, False, {}


def _archive_work_sessions(outdir: Path, log: Log) -> None:
    workdir = Path(outdir) / ".work"
    segments_dir = Path(outdir) / ".segments"
    if not workdir.is_dir():
        return
    segments_dir.mkdir(parents=True, exist_ok=True)
    for session_dir in sorted(workdir.glob("session_*"), key=lambda path: path.name):
        if not session_dir.is_dir():
            continue
        destination = segments_dir / session_dir.name
        if destination.exists():
            index = 2
            while (segments_dir / "{}_recovered{}".format(session_dir.name, index)).exists():
                index += 1
            destination = segments_dir / "{}_recovered{}".format(session_dir.name, index)
        shutil.move(str(session_dir), str(destination))
        log.info("Recovered raw session archived under {}".format(destination))
    try:
        workdir.rmdir()
    except OSError:
        pass


def recover_session_audio(session_path: Path) -> int:
    """Idempotently rebuild audio artifacts from a stopped session on disk."""
    outdir = Path(session_path).expanduser().resolve()
    if not outdir.is_dir():
        print("ERROR: session folder does not exist: {}".format(outdir), file=sys.stderr)
        return 1
    log = Log(outdir / "capture.log")
    recovery_work = outdir / ".recovery_work"
    recovery_work.mkdir(parents=True, exist_ok=True)
    expected_mic, expected_system, previous_sources = _load_integrity_expectations(outdir)
    mic_segments = raw_segment_paths(outdir, "mic")
    sys_segments = raw_segment_paths(outdir, "sys")
    if not expected_mic and not expected_system:
        expected_mic = bool(mic_segments)
        expected_system = bool(sys_segments)
    log.info("Recovering session audio from {}".format(outdir))

    merged_mic = outdir / "recording_mic.wav" if mic_segments else None
    merged_sys = outdir / "recording_sys.wav" if sys_segments else None
    if mic_segments and not merged_mic.is_file():
        if not merge_segments(mic_segments, merged_mic, recovery_work, log):
            write_report(outdir / "recording_integrity.json", build_report(
                session=outdir, state="capture_failed", expected_sources={
                    "mic": expected_mic, "system": expected_system},
                sources={}, phase="recovery", reason="microphone recovery merge failed"))
            log.close()
            return 1
    if sys_segments and not merged_sys.is_file():
        if not merge_segments(sys_segments, merged_sys, recovery_work, log):
            log.warn("System recovery merge failed; microphone recovery will be retained.")
            merged_sys = None

    verify_mic = verify_recording(merged_mic, mic_segments, log, "mic") if merged_mic else None
    verify_sys = verify_recording(merged_sys, sys_segments, log, "system") if merged_sys else None
    source_entries = {
        "mic": source_record(
            expected=expected_mic, segments=mic_segments, merged=merged_mic,
            verification=verify_mic,
            selected_device=(previous_sources.get("mic", {}) or {}).get("selected_device"),
            session_root=outdir,
        ),
        "system": source_record(
            expected=expected_system, segments=sys_segments, merged=merged_sys,
            verification=verify_sys,
            selected_device=(previous_sources.get("system", {}) or {}).get("selected_device"),
            session_root=outdir,
        ),
    }
    expected_entries = [entry for name, entry in source_entries.items()
                        if {"mic": expected_mic, "system": expected_system}[name]]
    if expected_entries and all(entry["state"] == "captured" for entry in expected_entries):
        state = "recovered"
    elif any(entry["state"] == "captured" for entry in expected_entries):
        state = "partial"
    else:
        state = "capture_failed"
    _archive_work_sessions(outdir, log)
    archived_mic = raw_segment_paths(outdir, "mic")
    archived_sys = raw_segment_paths(outdir, "sys")
    source_entries["mic"]["segments"] = [str(path.relative_to(outdir)) for path in archived_mic]
    source_entries["system"]["segments"] = [str(path.relative_to(outdir)) for path in archived_sys]
    write_report(outdir / "recording_integrity.json", build_report(
        session=outdir, state=state, expected_sources={
            "mic": expected_mic, "system": expected_system},
        sources=source_entries, phase="recovery",
        reason="recovered from raw segments" if state == "recovered" else
        "one or more expected sources could not be recovered"))
    if merged_mic and verify_mic:
        os.chmod(merged_mic, 0o444)
        append_manifest(outdir.parent.parent, outdir, merged_mic, verify_mic, len(mic_segments), log)
    if merged_sys and verify_sys:
        os.chmod(merged_sys, 0o444)
        append_manifest(outdir.parent.parent, outdir, merged_sys, verify_sys, len(sys_segments), log)
    shutil.rmtree(recovery_work, ignore_errors=True)
    log.info("Recovery finished: {}".format(state))
    log.close()
    return 0 if state in ("recovered", "partial") else 1


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
    easy to miss when the window is not being watched). Disabled by
    --offline / --no-notifications."""
    if not _NOTIFICATIONS:
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             'display notification "{}" with title "{}"'.format(
                 message.replace('"', "'"), title)],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def critical_alert(message: str, title: str = "zoom-recorder: attention needed") -> None:
    """Show a blocking macOS alert asynchronously for a capture failure.

    The recorder must keep its capture loop alive while the user reads the
    alert, so this deliberately runs in a short-lived background thread. It is
    separate from ordinary notifications because a silent other-party track
    is a correctness failure, not a status detail.
    """
    if not _NOTIFICATIONS:
        return

    def _show() -> None:
        try:
            script = ('display alert "{}" message "{}" as critical '
                      'buttons {"OK"} default button "OK"').format(
                          title.replace('"', "'")[:160], message.replace('"', "'")[:900])
            subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=300)
        except Exception:
            pass

    threading.Thread(target=_show, name="capture-alert", daemon=True).start()


def alert(label: str, coverage: float) -> None:
    """Loud, immediate signal that a recording came out mostly silent --
    this exact failure mode sat unnoticed in a transcript for weeks before."""
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass
    if not _NOTIFICATIONS:
        return
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

    # silencedetect does not always emit silence_end when silence runs through
    # EOF. Without this closeout, an entirely silent track can look like 100%
    # signal simply because its final silence interval was left open.
    if pending_start is not None and duration > pending_start:
        silences.append((pending_start, duration - pending_start))

    total_silence = min(sum(d for _, d in silences), duration) if duration > 0 else 0.0
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


def build_mixed_copy(mic_path: Optional[Path], sys_path: Optional[Path], out_path: Path, log: Log) -> Optional[Path]:
    """Build a mixed-down copy for transcription only. The originals stay
    untouched and separate; this file lives under derived/ and is never
    treated as a source of truth."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if mic_path is not None and mic_path.is_file() and sys_path is not None and sys_path.is_file():
        cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(mic_path), "-i", str(sys_path),
               "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest[a]",
               "-map", "[a]", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(out_path)]
    else:
        source = mic_path if (mic_path is not None and mic_path.is_file()) else sys_path
        if source is None or not source.is_file():
            return None
        cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(source),
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
                 "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin".format(cfg.model))
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


def resolve_recording_mode(system_only: bool, no_system: bool,
                           default_mode: str = "both") -> str:
    """Pure: CLI flags win over the configured default recording mode."""
    if system_only:
        return "system"
    if no_system:
        return "mic"
    return default_mode if default_mode in ("both", "mic", "system") else "both"


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
    parser.add_argument("--system-only", action="store_true",
                        help="record the other party only (no microphone)")
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
    parser.add_argument("--model", default=None,
                        help="whisper model path (default: from config, else "
                             "~/.cache/whisper-cpp/ggml-large-v3-turbo-q5_0.bin)")
    parser.add_argument("--basedir", default=None,
                        help="output base directory (default: from config)")
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
    parser.add_argument("--doctor", action="store_true",
                        help="check the environment (tools, BlackHole, routing, mic) "
                             "and print fixes, then exit")
    parser.add_argument("--recover", metavar="SESSION_DIR",
                        help="rebuild audio artifacts from a stopped session's raw segments, then exit")
    parser.add_argument("--system-capture", default=os.environ.get("ZOOM_SYSTEM_CAPTURE", "auto"),
                        choices=["auto", "tap", "loopback"],
                        help="how to capture system audio: loopback (BlackHole/Multi-Output, "
                             "safe fallback) or tap (Core Audio process tap; used automatically "
                             "when this context has System Audio Recording permission)")
    parser.add_argument("--offline", action="store_true",
                        help="privacy: block every non-loopback network call (live STT/"
                             "answers/KB embeddings) and turn notifications off")
    parser.add_argument("--no-notifications", action="store_true",
                        help="do not post desktop notifications")

    # -- live HUD (opt-in; recording is unchanged when these are not used) ---
    parser.add_argument("--live", action="store_true",
                        help="open the live transcript + AI answer HUD during recording")
    parser.add_argument("--hud-port", type=int, default=None,
                        help="port for the local HUD (default: random free port)")
    parser.add_argument("--transcript-dir", default=None,
                        help="additional folder for a live Markdown transcript mirror")
    parser.add_argument("--no-hud-browser", action="store_true",
                        help="do not auto-open the HUD in a browser")
    parser.add_argument("--live-no-answers", action="store_true",
                        help="live transcript only; never call an answer provider")
    parser.add_argument("--no-live-summary", action="store_true",
                        help="do not generate an end-of-call summary/action items/email")
    parser.add_argument("--live-audio-file", default=None,
                        help="feed a media file to the HUD instead of a live tap (testing)")
    parser.add_argument("--stt-backend", default=None,
                        help="live STT backend: local | groq | openai (local is default)")
    parser.add_argument("--stt-model", default=None, help="override the STT model")
    parser.add_argument("--stt-chunk-seconds", type=float, default=None,
                        help="live STT chunk length in seconds (default 5)")
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
    parser.add_argument("--diarize", action="store_true",
                        help="compatibility override: enable post-call WhisperX speaker splitting")
    parser.add_argument("--diarization-backend", default=None,
                        help="post-call attribution backend: auto | whisperx | off")
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
    if args.transcript_dir:
        cfg.transcript_writeback_dir = args.transcript_dir
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
        cfg.kb_next_dirs = list(args.kb_dir)
    elif cfg.kb_next_dirs:
        # The Settings UI can prepare a meeting-scoped source selection. It
        # overrides the permanent library for this live session only.
        cfg.kb_dirs = list(cfg.kb_next_dirs)
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
    if args.diarize:
        cfg.diarization_enabled = True
    if args.diarization_backend:
        cfg.diarization_backend = args.diarization_backend
    if getattr(args, "offline", False):
        # Offline is a hard privacy switch: no answers, no KB embeddings, and
        # STT only through a local backend (remote providers would be blocked
        # by the HTTP client anyway; this makes the intent explicit).
        from hud.config import LOCAL_STT_BACKENDS
        cfg.offline = True
        cfg.notifications = False
        cfg.answers_enabled = False
        cfg.kb_enabled = False
        if (cfg.stt_backend or "").lower() not in LOCAL_STT_BACKENDS:
            cfg.stt_backend = "local"
    return cfg


def resolve_system_capture(args: argparse.Namespace) -> str:
    """Decide how system audio is captured.

    An authorized process-tap context is the preferred automatic path because
    it follows the app's actual output without requiring a Multi-Output route.
    Unprivileged/background contexts stay on the loopback path, which remains
    the proven fallback and is also forced when the caller pins a device.
    """
    if args.system_capture == "tap" and not args.system:
        return "tap"
    if args.system_capture == "auto" and not args.system:
        try:
            from hud.system_tap import usable_in_this_context
            if usable_in_this_context():
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
    mic_device = rec.mic.device if rec.mic else None
    if use_system:
        system = choose_system(cfg, sys_cands, log)
        if system is None:
            log.warn("No loopback input available after fallback; recording microphone only.")
            rec.start(mic_device, None)
        else:
            rec.start(mic_device, system.device)
    else:
        rec.start(mic_device, None)
    return sys_cands, inputs


def recorder_lock_path(basedir: Path) -> Path:
    return Path(basedir) / ".zoom-recorder.lock"


def recorder_running(basedir: Path) -> bool:
    """True when another recorder holds the lock for this recordings folder."""
    lock = recorder_lock_path(basedir)
    if not lock.is_file():
        return False
    try:
        pid = int(lock.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def acquire_recorder_lock(basedir: Path) -> bool:
    lock = recorder_lock_path(basedir)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def release_recorder_lock(basedir: Path) -> None:
    lock = recorder_lock_path(basedir)
    try:
        if lock.is_file() and lock.read_text().strip() == str(os.getpid()):
            lock.unlink(missing_ok=True)
    except OSError:
        pass


def wait_for_first_segment(rec: "Recorder", timeout: float = 6.0,
                           expect_mic: bool = True) -> bool:
    """True once ffmpeg has written its first segment (capture actually
    flowing). A hung avfoundation open produces a live-but-silent ffmpeg that
    would otherwise go unnoticed until the end of the meeting."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if expect_mic:
            if rec.segments_mic():
                return True
        elif rec.segments_sys():
            return True
        if rec.proc is None or rec.proc.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def startup_audio_self_check(cfg: Config, rec: Recorder,
                             mic: Optional[Candidate], system: Optional[Candidate],
                             inputs: List[Device], log: Log) -> Optional[Candidate]:
    """Confirm live signal after capture starts and make one route recovery attempt.

    Device enumeration and a successful ffmpeg open are insufficient evidence:
    a loopback can be open while receiving digital silence. We only escalate
    when the mic is active too, so starting a recording in a quiet room does
    not create a false emergency. The monitor continues this check throughout
    the call after this bounded startup pass.
    """
    if rec.system is None or system is None:
        rec.set_source_state("system", "unavailable",
                             "no system capture source was selected")
        return system
    active_system = system
    max_wait = cfg.probe_seconds + 2.5
    for attempt in range(2):
        sys_probe = probe_level(active_system.device, cfg.probe_seconds,
                                max_wait=max_wait)
        if has_signal(sys_probe, cfg.silence_db):
            log.info("Audio self-check passed: system input '{}' carries live signal.".format(
                active_system.device.name))
            rec.set_system_warning("")
            rec.set_route_context({**rec.route_context,
                                   "application_route_verified": True,
                                   "signal_verified_device": active_system.device.name})
            rec.set_source_state("system", "captured", "startup signal confirmed",
                                 active_system.device.name)
            return active_system
        # The mic was already probed before the recorder opened it. Reopening
        # the active avfoundation mic here can wedge the real capture stream,
        # so use that cached startup evidence instead of a competing reader.
        if mic is None or mic.probe is None or not has_signal(mic.probe, cfg.silence_db):
            log.warn("Audio self-check is inconclusive: no live microphone signal was "
                     "present to compare against the silent loopback.")
            rec.set_source_state("system", "awaiting_signal",
                                 "loopback opened but no comparison signal was present",
                                 active_system.device.name)
            return active_system
        log.warn("Audio self-check: mic is active but system input '{}' is silent "
                 "(attempt {}/2).".format(active_system.device.name, attempt + 1))
        if cfg.system_capture != "loopback" or cfg.system_override or load_topology is None:
            break
        try:
            from hud.routing_fix import fix_routing
            route_output = rec.route_context.get("physical_output")
            result = fix_routing(
                load_topology(force=True),
                physical_output=(str(route_output) if route_output else None),
                assume_yes=True)
            log.info("Audio self-check route repair: {}".format(
                result.message.splitlines()[0] if result.message else result.ok))
        except Exception as exc:  # noqa: BLE001 - recording continues if repair is unavailable
            log.warn("Audio self-check route repair failed: {}".format(exc))
        refreshed = load_topology(force=True)
        retry_cands = build_system_candidates(inputs, refreshed)
        retry = choose_system(cfg, retry_cands, log)
        if retry is not None:
            if retry.device.name != active_system.device.name:
                log.warn("Audio self-check switching system input '{}' -> '{}'.".format(
                    active_system.device.name, retry.device.name))
                rec.start(mic.device if mic else None, retry.device,
                          pcm_source=rec.pcm_source)
            active_system = retry
            if has_signal(retry.probe, cfg.silence_db):
                rec.set_route_context({**rec.route_context,
                                       "application_route_verified": True,
                                       "signal_verified_device": active_system.device.name})
                rec.set_source_state("system", "captured", "route retry signal confirmed",
                                     active_system.device.name)
        time.sleep(0.5)
    warning = ("CRITICAL: Other-party audio has not been confirmed. The microphone is "
               "active, but the selected system/loopback input is silent. Set the call "
               "app's speaker to zoom-recorder Multi-Output, then retry the route.")
    rec.set_system_warning(warning)
    rec.set_route_context({**rec.route_context,
                           "application_route_verified": False,
                           "signal_verified_device": None})
    rec.set_source_state("system", "silent", warning, active_system.device.name)
    log.warn(warning)
    critical_alert(
        "The microphone is active, but the system/loopback track is silent. Other-party "
        "audio is not being captured. Set the meeting app's speaker to the managed "
        "Multi-Output device, then retry the route.", title="zoom-recorder: audio capture problem")
    return active_system


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    # Privacy switches first: they must be in force before anything can talk
    # to the network or pop a notification.
    set_notifications(not (args.offline or args.no_notifications))
    if args.offline:
        try:
            from hud.llm import set_offline
            set_offline(True)
        except Exception:  # noqa: BLE001
            pass
    if args.doctor:
        try:
            from hud.doctor import run_doctor
        except Exception as exc:  # noqa: BLE001
            print("ERROR: doctor unavailable: {}".format(exc), file=sys.stderr)
            return 1
        return 0 if run_doctor(args.probe_seconds) else 1
    if args.fix_routing or args.restore_routing:
        return run_routing_fix(args)
    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found. Install it: brew install ffmpeg", file=sys.stderr)
        return 1
    if not shutil.which("ffprobe"):
        print("ERROR: ffprobe not found. Install it: brew install ffmpeg", file=sys.stderr)
        return 1
    if args.recover:
        return recover_session_audio(Path(args.recover))

    inputs, outputs = list_devices()
    topo = load_topology(force=True) if load_topology is not None else None
    # A force-quit or crash can leave the tool-owned aggregate selected as the
    # default. Repair that before resolving the next capture mode, so tap mode
    # starts with ordinary physical-output behavior and working volume keys.
    try:
        from hud.routing_fix import recover_route_session
        recovered = recover_route_session(topo)
        if recovered.changed:
            print("Audio routing recovery: {}".format(recovered.message))
            topo = load_topology(force=True) if load_topology is not None else topo
    except Exception as exc:  # noqa: BLE001 - capture can still proceed
        print("Audio routing recovery skipped: {}".format(exc), file=sys.stderr)
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

    # Recorder-side defaults from the config file (CLI flags still win).
    try:
        from hud.config import recorder_defaults
        rdef = recorder_defaults()
    except Exception:  # noqa: BLE001
        rdef = None
    if rdef is not None:
        if args.basedir is None:
            args.basedir = rdef.basedir
        if args.model is None:
            args.model = rdef.transcription_model
        if args.mic is None:
            args.mic = rdef.mic
        if rdef.notifications is False:
            set_notifications(False)
        if rdef.offline and not args.offline:
            args.offline = True
            try:
                from hud.llm import set_offline
                set_offline(True)
            except Exception:  # noqa: BLE001
                pass
    mode = resolve_recording_mode(
        args.system_only, args.no_system,
        rdef.mode if rdef is not None else "both")
    record_mic = mode in ("both", "mic")
    record_system = mode in ("both", "system")

    basedir = Path(os.path.expanduser(args.basedir or "~/ZoomRecordings"))

    mic_cands = build_mic_candidates(inputs, topo) if record_mic else []
    system_cands = build_system_candidates(inputs, topo) if record_system else []

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
    # Segments are written under the session folder itself (not a temp dir) so
    # a crash or kill -9 leaves them recoverable next to capture.log. On a
    # clean finish they are moved to .segments/ and .work is removed.
    workdir = outdir / ".work"
    workdir.mkdir(parents=True, exist_ok=True)
    log = Log(outdir / "capture.log")

    try:
        free_gb = shutil.disk_usage(basedir).free / 1e9
        if free_gb < 2.0:
            log.warn("Low disk space: {:.1f} GB free in {} — long recordings may "
                     "fail.".format(free_gb, basedir))
            notify_user("Low disk space ({:.1f} GB free) — long recordings may "
                        "fail.".format(free_gb), title="zoom-recorder: disk space")
    except OSError:
        pass

    cfg = Config(
        segment_seconds=segment_seconds,
        chunk_seconds=args.chunk_seconds,
        fail_threshold=args.fail_threshold,
        cycle_seconds=args.cycle_seconds,
        probe_seconds=args.probe_seconds,
        silence_db=args.silence_db,
        mic_override=args.mic,
        system_override=args.system,
        use_system=record_system,
        transcribe=not args.no_transcribe,
        model=Path(os.path.expanduser(args.model or DEFAULT_MODEL)),
        basedir=basedir,
        outdir=outdir,
        workdir=workdir,
        system_capture=system_capture,
        record_mic=record_mic,
    )

    integrity_started_at = started.isoformat()

    routing_context = {}

    def persist_integrity(state: str, phase: str,
                          sources=None, reason: str = "") -> None:
        """Persist truthful session state even when normal finalization fails."""
        entries = sources or {
            "mic": {"expected": record_mic, "state": "unknown" if record_mic else "not_requested"},
            "system": {"expected": record_system, "state": "unknown" if record_system else "not_requested"},
        }
        report = build_report(
            session=outdir,
            state=state,
            expected_sources={"mic": record_mic, "system": record_system},
            sources=entries,
            phase=phase,
            reason=reason,
            started_at=integrity_started_at,
            routing=routing_context,
        )
        try:
            write_report(outdir / "recording_integrity.json", report)
        except OSError as exc:
            log.warn("Could not persist recording integrity report: {}".format(exc))

    persist_integrity("starting", "startup")

    # Write a recoverable identity immediately. The final identity is enriched
    # from the transcript when the session closes, but a crash or force-quit
    # should still leave a useful timestamped record behind.
    try:
        from hud.identity import build_identity, write_identity
        write_identity(outdir / "session.json",
                       build_identity(started, None, outdir.name, "", cfg))
    except (ImportError, OSError, TypeError, ValueError) as exc:
        log.warn("Could not write initial session identity: {}".format(exc))

    log.info("zoom-recorder starting. Output: {}".format(outdir))
    log.info("Inputs detected: {}".format(", ".join(d.name for d in inputs) or "(none)"))
    log.info("Outputs detected: {}".format(", ".join(d.name for d in outputs) or "(none)"))
    log.info("Recording mode: {}".format(
        {"both": "microphone + other party", "mic": "microphone only",
         "system": "other party only"}[mode]))

    mic = None
    if record_mic:
        if not mic_cands:
            log.error("No microphone inputs found.")
            persist_integrity("capture_failed", "source_selection", reason="no microphone inputs found")
            log.close()
            return 1
        mic = choose_mic(cfg, mic_cands, log, topo=topo)
        if mic is None:
            log.error("Could not select a usable microphone.")
            persist_integrity("capture_failed", "source_selection", reason="could not select a microphone")
            log.close()
            return 1

    system = None
    pcm_source = None
    route_session_started = False
    if cfg.use_system and not cfg.system_override:
        try:
            from hud.routing_fix import begin_route_session
            route_result = begin_route_session(cfg.system_capture, topo)
            # The marker is intentionally considered active even if the tap
            # restore failed; the next launch must still get a chance to heal
            # the route, and normal teardown should retry it.
            route_session_started = True
            if route_result.message:
                log.info("Audio routing: {}".format(route_result.message))
            if route_result.changed:
                topo = load_topology(force=True) if load_topology is not None else topo
                system_cands = build_system_candidates(inputs, topo)
        except Exception as exc:  # noqa: BLE001 - route recovery is best effort
            log.warn("Audio routing session setup skipped ({}).".format(exc))
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
                    preferred_output = None
                    if topo is not None:
                        current_output = topo.device(topo.default_output)
                        if (current_output is not None
                                and current_output.output_channels > 0
                                and not current_output.is_virtual
                                and not current_output.is_aggregate):
                            preferred_output = current_output.name
                    result = fix_routing(topo, physical_output=preferred_output,
                                         assume_yes=True)
                    detail = result.message.splitlines()[0] if result.message else ""
                    if result.ok:
                        log.info("Routing: {}".format(
                            detail or ("Multi-Output route ready" if result.changed
                                       else "existing loopback route verified")))
                        topo = load_topology(force=True)
                    else:
                        log.warn("Loopback routing setup failed: {}".format(
                            detail or "unknown routing error"))
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
                if record_mic and record_system:
                    critical_alert(
                        "The microphone is available, but no system/loopback source was found. "
                        "Other-party audio will not be captured. Fix Audio Routing or choose "
                        "microphone-only mode explicitly before relying on this recording.",
                        title="zoom-recorder: other-party audio unavailable")

    if mic is not None:
        log.info("Selected mic: {} (priority {}, level {})".format(
            mic.device.name, mic.priority,
            fmt_db(mic.probe.max_db) if mic.probe and mic.probe.ok else "n/a"))
    else:
        log.info("No microphone selected (system-only recording).")
    if system is not None:
        log.info("Selected system input: {}".format(system.device.name))
    if mic is None and system is None:
        log.error("No audio source available for the selected recording mode.")
        persist_integrity("capture_failed", "source_selection", reason="no audio source available")
        log.close()
        return 1

    persist_integrity("starting", "source_selection", {
        "mic": {"expected": record_mic, "state": "unknown" if record_mic else "not_requested",
                 "selected_device": mic.device.name if mic else None},
        "system": {"expected": record_system, "state": "unknown" if record_system else "not_requested",
                    "selected_device": system.device.name if system else None},
    })

    rec = Recorder(cfg, log)
    runtime_sources = {
        "mic": {"expected": record_mic, "state": "unknown",
                 "selected_device": mic.device.name if mic else None},
        "system": {"expected": record_system, "state": "unknown",
                    "selected_device": system.device.name if system else None},
    }

    def on_integrity_update(source: str, state: str, reason: str,
                            device: Optional[str]) -> None:
        entry = runtime_sources.setdefault(source, {"expected": True})
        entry.update({"state": state, "reason": reason or None,
                      "selected_device": device or entry.get("selected_device")})
        expected = [value for value in runtime_sources.values() if value.get("expected")]
        if expected and all(value.get("state") == "captured" for value in expected):
            overall = "healthy"
        elif any(value.get("state") == "captured" for value in expected):
            overall = "partial"
        elif any(value.get("state") in ("silent", "unavailable", "failed", "degraded")
                 for value in expected):
            overall = "degraded"
        else:
            overall = "starting"
        persist_integrity(overall, "runtime", runtime_sources, reason=reason)

    live_holder = {"session": None}

    def on_route_update(context: dict) -> None:
        routing_context.clear()
        routing_context.update(context)
        if live_holder["session"] is not None:
            try:
                live_holder["session"].set_audio_route_context(context)
            except Exception:
                pass
        persist_integrity("runtime", "routing", runtime_sources,
                          reason="audio route changed")

    rec.on_integrity_update = on_integrity_update
    rec.on_route_update = on_route_update
    try:
        from hud.routing_fix import route_snapshot
        routing_context.update(route_snapshot(
            topo, system.device.name if system else (pcm_source.name() if pcm_source else None),
            "core_audio_tap" if cfg.system_capture == "tap" else "loopback"))
        rec.set_route_context(routing_context)
    except Exception as exc:  # noqa: BLE001 - routing facts are diagnostic only
        log.warn("Could not snapshot audio routing: {}".format(exc))
    stop = threading.Event()

    def handle_signal(signum, frame):
        stop.set()

    def request_stop():
        """Share a stop request with the menu bar before finalization begins."""
        stop.set()
        try:
            STOP_REQUEST_FILE.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass

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
    live_cfg = None
    if args.live:
        try:
            from hud.session import LiveSession

            hud_cfg = build_hud_config(args)
            live_cfg = hud_cfg
            live = LiveSession(
                hud_cfg, outdir, log.info,
                mic.device.name if mic is not None else None,
                system_name(),
                cfg.model,
                on_stop=request_stop,
                started_at=started,
            )
            live.start()
            live_holder["session"] = live
            live.set_audio_route_context(routing_context)
            rec.on_restart = lambda: live.update_devices(
                rec.mic.device.name if rec.mic else None,
                system_name())
            rec.on_system_warning = live.set_system_audio_warning
        except Exception as exc:  # noqa: BLE001
            log.warn("Live HUD unavailable ({}); recording continues normally.".format(exc))
            live = None

    if recorder_running(basedir):
        log.error("Another recording is already in progress in {} — refusing to "
                  "start a second one.".format(basedir))
        persist_integrity("capture_failed", "startup", reason="another recording is already in progress")
        log.close()
        return 1
    acquire_recorder_lock(basedir)

    mic_device = mic.device if mic is not None else None
    rec.start(mic_device, system.device if system else None, pcm_source=pcm_source)
    healthy = wait_for_first_segment(rec, expect_mic=record_mic)
    if not healthy:
        log.warn("No audio 6s after start; ffmpeg looks stuck. Restarting once.")
        log_ffmpeg_tail(rec, log)
        rec.start(mic_device, system.device if system else None, pcm_source=pcm_source)
        healthy = wait_for_first_segment(rec, expect_mic=record_mic)
    if not healthy:
        log_ffmpeg_tail(rec, log)
        if pcm_source is not None and record_mic:
            # Tap mode wedges the mic open in some process contexts
            # (launchd/GUI-spawned python on macOS 15). Continue the
            # recording on the proven loopback path instead of failing.
            log.warn("Mic capture wedged even after a restart; falling back "
                     "to loopback capture.")
            system_cands, inputs = fallback_tap_to_loopback(cfg, rec, log, cfg.use_system)
            system = None
            healthy = wait_for_first_segment(rec, expect_mic=record_mic)
    if not healthy:
        log.warn("Capture still not producing data; monitoring will keep "
                 "watching it.")
    if record_mic:
        rec.set_source_state(
            "mic", "captured" if healthy else "awaiting_signal",
            "first microphone segment observed" if healthy else
            "waiting for the first microphone segment",
            mic_device.name if mic_device else None,
        )
    if record_system:
        rec.set_source_state(
            "system", "awaiting_signal" if system is not None else "unavailable",
            "waiting for positive system-audio signal" if system is not None else
            "no system capture source selected",
            system.device.name if system else (pcm_source.name() if pcm_source else None),
        )
    if record_system and system is not None:
        system = startup_audio_self_check(cfg, rec, mic, system, inputs, log)
    log.info("Recording. Press Ctrl+C to stop and merge.")

    try:
        monitor(cfg, rec, inputs, mic_cands, system_cands, stop, log)
    except Exception as exc:  # noqa: BLE001
        log.error("Monitor loop crashed: {}".format(exc))
    finally:
        log.info("Stopping recording...")
        persist_integrity("stopping", "shutdown")
        rec.stop()
        release_recorder_lock(basedir)
        if pcm_source is not None:
            try:
                pcm_source.stop()
            except Exception as exc:  # noqa: BLE001
                log.warn("System tap shutdown error: {}".format(exc))
        # Hand the default output back to a physical device for both capture
        # modes. Loopback needs this after every recording; tap mode normally
        # leaves routing alone, but this also heals an aggregate left active
        # by an earlier session or a partial shutdown.
        if route_session_started:
            try:
                from hud.routing_fix import end_route_session
                restored = end_route_session(
                    load_topology(force=True) if load_topology is not None else None)
                if restored.changed:
                    log.info("Audio routing: {}".format(restored.message))
                elif not restored.ok:
                    log.warn("Could not restore the default output: {}".format(
                        restored.message))
            except Exception as exc:  # noqa: BLE001
                log.warn("Could not restore the default output ({})".format(exc))
        if live is not None:
            recorder_root_before_hud_stop = outdir
            try:
                live.stop()
            except Exception as exc:  # noqa: BLE001
                log.warn("Live HUD shutdown error: {}".format(exc))
            finally:
                # LiveSession may rename the numeric folder after deriving its
                # topic. All post-recording artifacts must follow that move,
                # even if a non-critical HUD cleanup step raised.
                outdir = live.outdir
            rec.rebind_outdir(recorder_root_before_hud_stop, outdir)
            workdir = outdir / ".work"
        # Disk is authoritative after every lifecycle boundary. This catches
        # a HUD rename, a restart, or a future shutdown refactor that forgets
        # to update Recorder.sessions.
        rec.reconcile_sessions(outdir)

        mic_segments = rec.segments_mic()
        sys_segments = rec.segments_sys()

        merged_mic: Optional[Path] = None
        if mic_segments:
            candidate_mic = outdir / "recording_mic.wav"
            mic_ok = merge_segments(mic_segments, candidate_mic, workdir, log)
            mic_duration = probe_duration(candidate_mic) if mic_ok else None
            if not mic_ok or (mic_duration is not None
                              and mic_duration < MIN_VALID_RECORDING_S):
                log.error("No usable mic audio was recorded{}.".format(
                    " (merged track is only {:.2f}s)".format(mic_duration)
                    if mic_duration is not None else ""))
                log_ffmpeg_tail(rec, log)
                preserve_session_diagnostics(rec, outdir, log)
                persist_integrity("capture_failed", "merge", reason="microphone merge failed or was too short")
                shutil.rmtree(workdir, ignore_errors=True)
                log.close()
                return 1
            merged_mic = candidate_mic

        merged_sys: Optional[Path] = None
        if sys_segments:
            candidate_sys = outdir / "recording_sys.wav"
            if merge_segments(sys_segments, candidate_sys, workdir, log):
                sys_duration = probe_duration(candidate_sys)
                if sys_duration is not None and sys_duration < MIN_VALID_RECORDING_S:
                    log.error("No usable system audio was recorded (merged track "
                              "is only {:.2f}s).".format(sys_duration))
                else:
                    merged_sys = candidate_sys
            else:
                log.warn("System track merge failed; continuing without it.")

        if merged_mic is None and merged_sys is None:
            log.error("No usable audio was recorded.")
            log_ffmpeg_tail(rec, log)
            preserve_session_diagnostics(rec, outdir, log)
            persist_integrity("capture_failed", "merge", reason="no usable merged audio artifact")
            shutil.rmtree(workdir, ignore_errors=True)
            log.close()
            return 1

        log.info("Verifying recording before archiving...")
        verify_mic = verify_recording(merged_mic, mic_segments, log, "mic") if merged_mic else None
        verify_sys = verify_recording(merged_sys, sys_segments, log, "system") if merged_sys else None

        source_entries = {
            "mic": source_record(
                expected=record_mic, segments=mic_segments, merged=merged_mic,
                verification=verify_mic,
                selected_device=mic.device.name if mic else None,
                session_root=outdir,
            ),
            "system": source_record(
                expected=record_system, segments=sys_segments, merged=merged_sys,
                verification=verify_sys,
                selected_device=system.device.name if system else system_name(),
                session_root=outdir,
            ),
        }
        expected_entries = [entry for name, entry in source_entries.items()
                            if {"mic": record_mic, "system": record_system}[name]]
        if expected_entries and all(entry["state"] == "captured" for entry in expected_entries):
            integrity_state = "healthy"
        elif any(entry["state"] == "captured" for entry in expected_entries):
            integrity_state = "partial"
        elif any(entry["state"] in ("silent", "degraded", "truncated")
                 for entry in expected_entries):
            integrity_state = "degraded"
        else:
            integrity_state = "capture_failed"
        persist_integrity(integrity_state, "verified", source_entries,
                          reason="one or more expected sources did not meet capture integrity"
                          if integrity_state != "healthy" else "")

        # Gap 1: never delete segments -- move them to .segments/, and lock the
        # merged originals read-only so nothing (including a future run of
        # this script) can silently overwrite the only copy.
        if merged_mic:
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

        archived_mic = sorted(segments_dest.glob("session_*/seg_*_mic.wav"))
        archived_sys = sorted(segments_dest.glob("session_*/seg_*_sys.wav"))
        source_entries["mic"] = source_record(
            expected=record_mic, segments=archived_mic, merged=merged_mic,
            verification=verify_mic, selected_device=mic.device.name if mic else None,
            session_root=outdir,
        )
        source_entries["system"] = source_record(
            expected=record_system, segments=archived_sys, merged=merged_sys,
            verification=verify_sys, selected_device=system.device.name if system else system_name(),
            session_root=outdir,
        )
        persist_integrity(integrity_state, "archived", source_entries,
                          reason="one or more expected sources did not meet capture integrity"
                          if integrity_state != "healthy" else "")

        # Gap 4: append-only checksum manifest.
        if merged_mic:
            append_manifest(basedir, outdir, merged_mic, verify_mic, len(mic_segments), log)
        if merged_sys:
            append_manifest(basedir, outdir, merged_sys, verify_sys, len(sys_segments), log)

        log.info("Saved: {} ({} mic segments, {} system segments)".format(
            " + ".join(str(p) for p in (merged_mic, merged_sys) if p),
            len(mic_segments), len(sys_segments)))

        mixed = build_mixed_copy(merged_mic, merged_sys,
                                 outdir / "derived" / "recording_mixed.wav", log)
        if mixed is not None:
            transcribe(cfg, mixed, log)

        # Optional post-call attribution is deliberately after capture, live
        # STT, answers, and the ordinary transcription path. It can never add
        # latency to the question loop or risk the original audio files.
        if live is not None and live_cfg is not None and live_cfg.diarization_enabled:
            try:
                from hud.diarization import run_post_call_diarization
                diar_audio = merged_sys or mixed
                if diar_audio is not None:
                    diar_result = run_post_call_diarization(
                        diar_audio, live.state.since(0), outdir / "derived", live_cfg,
                        log.info, started_epoch=started.timestamp(),
                        mappings=live.state.speaker_mappings())
                    if diar_result:
                        identity_path = outdir / "session.json"
                        try:
                            identity = json.loads(identity_path.read_text(encoding="utf-8"))
                            identity["diarization"] = {
                                "backend": diar_result.get("backend"),
                                "speaker_count": diar_result.get("speaker_count"),
                                "segments_file": "derived/diarization.json",
                            }
                            identity_path.write_text(
                                json.dumps(identity, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8")
                        except (OSError, ValueError) as exc:
                            log.warn("Diarization completed but session metadata was not updated: {}".format(exc))
            except Exception as exc:  # noqa: BLE001
                log.warn("Optional diarization failed; original transcript kept: {}".format(exc))

        log.info("Done. Originals are read-only under {}/. Derived copies (safe to overwrite) "
                 "belong in {}/derived/.".format(outdir, outdir))
        log.close()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
