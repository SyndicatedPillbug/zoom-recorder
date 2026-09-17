#!/usr/bin/env python3
"""macOS audio device topology and classification.

The recorder needs to tell three kinds of device apart:

  * a real **microphone** (the built-in mic, a USB/headset mic);
  * a **loopback** that carries *system output* (BlackHole, Loopback app,
    Soundflower) -- the only kind that can capture the other party;
  * a **virtual device that is not a system mirror** (notably Zoom's
    ``ZoomAudioDevice``, which only carries Zoom's own shared audio).

``system_profiler SPAudioDataType -json`` gives us the default input/output
flags and each device's transport, so we do not have to guess from names
alone. Everything here is stdlib and safe to import from both the recorder and
the HUD.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# A loopback that can carry arbitrary system audio (best first).
SYSTEM_PRIORITY = [
    (re.compile(r"blackhole|black\s*hole", re.I), 100),
    (re.compile(r"loopback", re.I), 90),
    (re.compile(r"soundflower", re.I), 80),
    (re.compile(r"multi-?output|aggregate", re.I), 70),
    (re.compile(r"zoom\s*audio\s*device", re.I), 20),
]

# Real microphones, best first.
MIC_PRIORITY = [
    (re.compile(r"external|usb|yet[ai]|blue\b|rode|shure|audio[\s-]?technica|samson|fifine|elgato|logitech", re.I), 60),
    (re.compile(r"built-?in|macbook|imac|studio display|display", re.I), 50),
    (re.compile(r"headset|airpods|beats|headphone|bluetooth", re.I), 30),
    (re.compile(r"iphone|continuity|desk view", re.I), 10),
]

LOOPBACK_RE = re.compile(
    r"zoom\s*audio\s*device|blackhole|black\s*hole|loopback|soundflower", re.I)
AGGREGATE_RE = re.compile(r"multi-?output|aggregate", re.I)


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class AudioDevice:
    name: str
    transport: str = ""
    input_channels: int = 0
    output_channels: int = 0
    default_input: bool = False
    default_output: bool = False

    @property
    def is_virtual(self) -> bool:
        return "virtual" in self.transport.lower()

    @property
    def is_aggregate(self) -> bool:
        return bool(AGGREGATE_RE.search(self.name)) or "aggregate" in self.transport.lower()

    @property
    def is_loopback(self) -> bool:
        return self.is_virtual or bool(LOOPBACK_RE.search(self.name)) or self.is_aggregate

    @property
    def is_mic(self) -> bool:
        return (self.input_channels > 0 and not self.is_virtual
                and not self.is_loopback and not self.is_aggregate)


@dataclass
class Topology:
    devices: List[AudioDevice] = field(default_factory=list)
    default_input: Optional[str] = None
    default_output: Optional[str] = None

    def device(self, name: Optional[str]) -> Optional[AudioDevice]:
        if not name:
            return None
        for dev in self.devices:
            if dev.name == name:
                return dev
        return None

    def loopbacks(self) -> List[AudioDevice]:
        return [d for d in self.devices if d.is_loopback]

    def mics(self) -> List[AudioDevice]:
        return [d for d in self.devices if d.is_mic]

    def looks_like_loopback(self, name: str) -> bool:
        dev = self.device(name)
        if dev is not None:
            return dev.is_loopback
        return bool(LOOPBACK_RE.search(name) or AGGREGATE_RE.search(name))

    def looks_like_mic(self, name: str) -> bool:
        dev = self.device(name)
        if dev is not None:
            return dev.is_mic
        return not self.looks_like_loopback(name)

    @property
    def system_in_output_path(self) -> bool:
        """True when a loopback is plausibly receiving the default output."""
        if not self.devices:
            return True  # topology unknown; don't block selection
        out = self.device(self.default_output)
        if out is not None:
            return out.is_loopback
        return bool(self.loopbacks())


_CACHE: Dict[str, object] = {"at": 0.0, "topo": None}


def load_topology(force: bool = False, ttl: float = 5.0) -> Topology:
    now = time.time()
    cached = _CACHE.get("topo")
    if not force and isinstance(cached, Topology) and now - float(_CACHE["at"]) < ttl:
        return cached
    topo = read_system_profiler()
    _CACHE["at"] = now
    _CACHE["topo"] = topo
    return topo


def read_system_profiler() -> Topology:
    """Parse ``system_profiler SPAudioDataType -json`` (never raises)."""
    try:
        proc = subprocess.run(
            ["system_profiler", "SPAudioDataType", "-json"],
            capture_output=True, text=True, timeout=8)
        data = json.loads(proc.stdout)
    except Exception:  # noqa: BLE001
        return Topology()

    try:
        items = data["SPAudioDataType"][0]["_items"]
    except (KeyError, IndexError, TypeError):
        return Topology()

    topo = Topology()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("_name") or "").strip()
        if not name:
            continue
        dev = AudioDevice(
            name=name,
            transport=str(item.get("coreaudio_device_transport") or ""),
            input_channels=_to_int(item.get("coreaudio_device_input")),
            output_channels=_to_int(item.get("coreaudio_device_output")),
            default_input=(item.get("coreaudio_default_audio_input_device") == "spaudio_yes"),
            default_output=(item.get("coreaudio_default_audio_output_device") == "spaudio_yes"),
        )
        topo.devices.append(dev)
        if dev.default_input:
            topo.default_input = dev.name
        if dev.default_output:
            topo.default_output = dev.name
    return topo


def system_priority(name: str) -> int:
    for pattern, score in SYSTEM_PRIORITY:
        if pattern.search(name):
            return score
    return 0


def mic_priority(name: str) -> int:
    for pattern, score in MIC_PRIORITY:
        if pattern.search(name):
            return score
    return 20


def system_advice(topo: Optional[Topology] = None) -> str:
    """Human-readable explanation of why system audio may not be captured."""
    topo = topo or load_topology()
    good = [d.name for d in topo.loopbacks() if system_priority(d.name) >= 70]
    if not good:
        return ("No general-purpose loopback found (only virtual devices such as "
                "ZoomAudioDevice, which carry Zoom's own audio). Install BlackHole "
                "(brew install blackhole-2ch) and add it to a Multi-Output Device so "
                "system audio can be captured.")
    if not topo.system_in_output_path:
        return ("System audio is not routed to a loopback. Create a Multi-Output "
                "Device that includes '{}' plus your speakers/headphones, then set "
                "it as the default output.".format(good[0]))
    out = topo.device(topo.default_output)
    if out is not None and out.is_virtual and not out.is_aggregate:
        # The inverse misroute: a bare loopback is the default output, so
        # audio reaches the recorder but never the user's ears.
        return ("The default output is '{}' by itself, so system audio is captured "
                "but you hear nothing. Create a Multi-Output Device that includes "
                "'{}' plus your speakers/headphones and select that as the default "
                "output.".format(out.name, good[0]))
    return ""
