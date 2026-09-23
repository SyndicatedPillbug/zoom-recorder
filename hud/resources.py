#!/usr/bin/env python3
"""Low-cost host-resource telemetry for live recordings.

The local Turbo model is intentionally kept as the primary transcription lane.
This module does not change that choice or throttle it.  It only observes the
host so a user can see when the recorder and a video call are competing for
memory, and so the session keeps an explainable diagnostic trail.

macOS exposes useful pressure data through ``memory_pressure`` and swap data
through ``sysctl``.  The small fallbacks keep unit tests and non-macOS replay
tools usable without adding a dependency such as psutil.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


_FREE_RE = re.compile(r"free percentage:\s*([\d.]+)%", re.I)
_SWAP_USED_RE = re.compile(r"used\s*=\s*([\d.]+)\s*([KMGTP])B?", re.I)
_PS_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)$")
_UNIT_MB = {"K": 1 / 1024, "M": 1.0, "G": 1024.0,
            "T": 1024.0 * 1024.0, "P": 1024.0 * 1024.0 * 1024.0}


@dataclass(frozen=True)
class ResourceSnapshot:
    free_percent: Optional[float] = None
    swap_used_mb: Optional[float] = None
    process_rss_mb: Optional[float] = None
    whisper_rss_mb: Optional[float] = None
    physical_memory_mb: Optional[float] = None
    pressure: str = "unknown"
    checked_at: float = 0.0


def parse_free_percent(text: str) -> Optional[float]:
    match = _FREE_RE.search(text or "")
    if not match:
        return None
    try:
        return max(0.0, min(100.0, float(match.group(1))))
    except ValueError:
        return None


def parse_swap_used_mb(text: str) -> Optional[float]:
    match = _SWAP_USED_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1)) * _UNIT_MB[match.group(2).upper()]
    except (KeyError, ValueError):
        return None


def parse_process_rows(text: str) -> List[Tuple[int, int, float, str]]:
    rows: List[Tuple[int, int, float, str]] = []
    for line in (text or "").splitlines():
        match = _PS_RE.match(line)
        if not match:
            continue
        try:
            rows.append((int(match.group(1)), int(match.group(2)),
                         float(match.group(3)) / 1024.0, match.group(4).strip()))
        except ValueError:
            continue
    return rows


def process_tree_memory(rows: Iterable[Tuple[int, int, float, str]],
                        root_pid: int) -> Tuple[float, float]:
    """Return (all descendant RSS MB, whisper descendant RSS MB)."""
    entries = list(rows)
    children: Dict[int, List[int]] = {}
    by_pid: Dict[int, Tuple[int, int, float, str]] = {}
    for pid, ppid, rss, command in entries:
        children.setdefault(ppid, []).append(pid)
        by_pid[pid] = (pid, ppid, rss, command)
    seen = set()
    stack = [int(root_pid)]
    total = 0.0
    whisper = 0.0
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        row = by_pid.get(pid)
        if row is not None:
            rss = row[2]
            total += rss
            if "whisper" in row[3].lower():
                whisper += rss
        stack.extend(children.get(pid, []))
    return total, whisper


def _run(command: List[str], timeout: float = 2.0) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout)
        return (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def collect_snapshot(pid: Optional[int] = None) -> ResourceSnapshot:
    """Collect a best-effort snapshot without raising into the recorder."""
    pid = int(pid or os.getpid())
    free_percent: Optional[float] = None
    swap_used: Optional[float] = None
    physical_mb: Optional[float] = None
    if sys.platform == "darwin":
        pressure_text = _run(["memory_pressure", "-Q"])
        free_percent = parse_free_percent(pressure_text)
        swap_used = parse_swap_used_mb(_run(["sysctl", "-n", "vm.swapusage"]))
        try:
            physical_mb = float(_run(["sysctl", "-n", "hw.memsize"]).strip()) / (1024 * 1024)
        except ValueError:
            physical_mb = None
        pressure = ("critical" if free_percent is not None and free_percent <= 10
                    else "warning" if free_percent is not None and free_percent <= 20
                    else "normal" if free_percent is not None else "unknown")
        rows = parse_process_rows(_run(["ps", "-axo", "pid=,ppid=,rss=,comm="]))
    else:
        pressure = "unknown"
        rows = parse_process_rows(_run(["ps", "-axo", "pid=,ppid=,rss=,comm="]))
        try:
            meminfo = open("/proc/meminfo", encoding="utf-8").read()
            total = re.search(r"^MemTotal:\s+(\d+)", meminfo, re.M)
            available = re.search(r"^MemAvailable:\s+(\d+)", meminfo, re.M)
            if total and available:
                physical_mb = float(total.group(1)) / 1024.0
                free_percent = 100.0 * float(available.group(1)) / float(total.group(1))
                pressure = ("critical" if free_percent <= 10 else
                            "warning" if free_percent <= 20 else "normal")
        except OSError:
            pass
    process_rss, whisper_rss = process_tree_memory(rows, pid)
    return ResourceSnapshot(
        free_percent=free_percent,
        swap_used_mb=swap_used,
        process_rss_mb=process_rss or None,
        whisper_rss_mb=whisper_rss or None,
        physical_memory_mb=physical_mb,
        pressure=pressure,
        checked_at=time.time(),
    )


def warning_for(snapshot: ResourceSnapshot) -> str:
    """Create an actionable warning, or an empty string when healthy."""
    free = snapshot.free_percent
    swap = snapshot.swap_used_mb
    if free is not None and free <= 10:
        details = "free memory {:.0f}%".format(free)
        if swap is not None and swap >= 1024:
            details += ", swap {:.1f} GB used".format(swap / 1024.0)
        return ("CRITICAL: macOS memory pressure is high ({}). Close unused browsers "
                "or other heavy apps now; local Turbo transcription remains active.".format(details))
    if (free is not None and free <= 20) or (swap is not None and swap >= 2048):
        details = []
        if free is not None and free <= 20:
            details.append("free memory {:.0f}%".format(free))
        if swap is not None and swap >= 2048:
            details.append("swap {:.1f} GB used".format(swap / 1024.0))
        return ("Memory pressure may affect the meeting and local Turbo ({}). "
                "Close unused browsers or other heavy apps.".format(", ".join(details)))
    return ""


class ResourceMonitor:
    """Publish periodic resource telemetry to the live HUD state."""

    def __init__(self, state: Any, log: Callable[[str], None], interval: float = 5.0,
                 sampler: Callable[[], ResourceSnapshot] = collect_snapshot) -> None:
        self.state = state
        self.log = log
        self.interval = max(1.0, float(interval))
        self.sampler = sampler
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_warning = ""
        self._last_logged_at = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hud-resources", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception as exc:  # noqa: BLE001 - telemetry never stops recording
                self.log("resource monitor unavailable: {}".format(exc))
            self._stop.wait(self.interval)

    def sample(self) -> ResourceSnapshot:
        snapshot = self.sampler()
        warning = warning_for(snapshot)
        meta: Dict[str, Any] = {
            "resource_pressure": snapshot.pressure,
            "resource_warning": warning,
            "resource_checked_at": snapshot.checked_at,
        }
        for key, value in (
                ("resource_memory_free_percent", snapshot.free_percent),
                ("resource_swap_used_mb", snapshot.swap_used_mb),
                ("resource_process_rss_mb", snapshot.process_rss_mb),
                ("resource_whisper_rss_mb", snapshot.whisper_rss_mb),
                ("resource_physical_memory_mb", snapshot.physical_memory_mb)):
            if value is not None:
                meta[key] = round(float(value), 1)
        self.state.set_meta(**meta)
        if snapshot.process_rss_mb is not None:
            self.state.observe_metric("resource_process_rss_mb", snapshot.process_rss_mb)
        if snapshot.whisper_rss_mb is not None:
            self.state.observe_metric("resource_whisper_rss_mb", snapshot.whisper_rss_mb)
        now = time.time()
        if warning and (warning != self._last_warning or now - self._last_logged_at >= 60.0):
            self.log(warning)
            self._last_logged_at = now
        self._last_warning = warning
        return snapshot
