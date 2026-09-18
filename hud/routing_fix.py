#!/usr/bin/env python3
"""Automated macOS system-audio routing fix for loopback capture.

The recorder can only capture the other party through a loopback device
(BlackHole, the Loopback app, ...) that sits in the system's output path.
The usual fix -- creating a Multi-Output Device in Audio MIDI Setup and
selecting it as the default output -- is fully scriptable via public
CoreAudio APIs, so this module does it directly:

  1. ``AudioHardwareCreateAggregateDevice`` with the "stacked" flag is
     exactly what Audio MIDI Setup's "Create Multi-Output Device" does.
     The new device plays to every subdevice, so your speakers/headphones
     AND the loopback both receive system audio.
  2. ``kAudioHardwarePropertyDefaultOutputDevice`` selects it as the
     system default output. No sudo, no accessibility permission, no
     GUI scripting -- both are ordinary per-user CoreAudio calls.

If anything unexpected happens (device names not resolvable, CoreAudio
errors), ``fix_routing`` returns the click-by-click manual walkthrough
instead, so the app always has a path forward.

Everything is stdlib (ctypes against CoreAudio/CoreFoundation). Run as a
script for a standalone fix: ``python3 -m hud.routing_fix``.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

try:
    from .devices import AudioDevice, Topology, read_system_profiler, system_priority
except ImportError:  # run as a plain script from the repo root
    from hud.devices import AudioDevice, Topology, read_system_profiler, system_priority  # type: ignore

# The multi-output device this module creates/owns (stable name -> we can
# find and update it on later runs).
MULTI_OUTPUT_NAME = "zoom-recorder Multi-Output"

# Where the last successful pairing is remembered (so rebuilds track the
# device you actually listen on instead of re-guessing).
STATE_PATH = Path.home() / ".zoom_recorder_routing.json"

# Real output devices, best first (this is what plays to your ears).
PHYSICAL_OUTPUT_PRIORITY = [
    (re.compile(r"headphone|headset|airpods|beats", re.I), 60),
    (re.compile(r"speaker", re.I), 50),
    (re.compile(r"built-?in|studio display", re.I), 40),
    (re.compile(r"hdmi|displayport|display|thunderbolt|usb", re.I), 30),
]

# Four-char codes (FourCC) for CoreAudio property selectors.
_DEVICE_LIST = int.from_bytes(b"dev#", "big")
_DEVICE_NAME = int.from_bytes(b"lnam", "big")
_DEVICE_UID = int.from_bytes(b"uid ", "big")
_DEFAULT_OUTPUT = int.from_bytes(b"dOut", "big")
_DEFAULT_INPUT = int.from_bytes(b"dIn ", "big")
_VOLUME_SCALAR = int.from_bytes(b"volm", "big")
_MUTE = int.from_bytes(b"mute", "big")
_OUTPUT_SCOPE = int.from_bytes(b"outp", "big")
_SYSTEM_OBJECT = 1


class RoutingError(Exception):
    """Raised when the CoreAudio backend cannot complete an operation."""


@dataclass
class FixResult:
    ok: bool
    changed: bool
    message: str


@dataclass
class CaDevice:
    object_id: int
    name: str
    uid: str


# ---------------------------------------------------------------- pure logic

def physical_output_priority(name: str) -> int:
    for pattern, score in PHYSICAL_OUTPUT_PRIORITY:
        if pattern.search(name):
            return score
    return 10


def clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def stepped_volume(current: float, delta: float) -> float:
    return clamp_percent(float(current) + float(delta))


def format_volume_label(percent: float, muted: bool = False) -> str:
    if muted:
        return "Volume: muted"
    return "Volume: {:.0f}%".format(clamp_percent(percent))


def format_volume_title(percent: float, muted: bool = False) -> str:
    if muted:
        return "Volume (muted)"
    return "Volume {:.0f}%".format(clamp_percent(percent))


def walkthrough(loopback: str, physical: str, multi: str) -> str:
    return """\
Click-by-click manual setup (only needed if automation failed):

  1. Open Audio MIDI Setup (Spotlight search "Audio MIDI Setup").
  2. Click the "+" button at the bottom of the device list (left panel).
  3. Choose "Create Multi-Output Device".
  4. In the right panel, tick BOTH "{loopback}" AND "{physical}".
  5. Tick "Drift Correction" on the "{loopback}" row.
  6. Right-click the "{physical}" row and choose "Use As Master Device"
     (it should be the Primary/master device).
  7. The device is named "Multi-Output Device"; rename it to "{multi}"
     if you want it to match what this tool expects (optional).
  8. Open System Settings > Sound > Output (or run:
     open "x-apple.systempreferences:com.apple.Sound-Settings.extension")
     and select "{multi}" as the output device.
  9. In Zoom: Settings > Audio > Speaker -> select "{multi}" too, so call
     audio also reaches the loopback.
 10. Verify: ./zoom_record.py --self-test

Note: a Multi-Output Device plays to BOTH devices but the volume keys
usually stop working for it; set levels once in Audio MIDI Setup. If you
switch between headphones and speakers, re-run this fix (or use the
menu-bar "Audio Out" dropdown) to rebuild the multi-output around the
device you actually listen on.""".format(loopback=loopback, physical=physical, multi=multi)


# ------------------------------------------------------- CoreAudio (ctypes)

class _CoreAudio:
    """Lazy ctypes bindings; raises RoutingError when unavailable."""

    def __init__(self) -> None:
        try:
            cf_path = ctypes.util.find_library("CoreFoundation")
            ca_path = ctypes.util.find_library("CoreAudio")
            if not cf_path or not ca_path:
                raise RoutingError("CoreFoundation/CoreAudio framework not found")
            self.cf = ctypes.CDLL(cf_path, use_errno=True)
            self.ca = ctypes.CDLL(ca_path, use_errno=True)
        except OSError as exc:
            raise RoutingError("cannot load CoreAudio: {}".format(exc))

        c_void_p = ctypes.c_void_p
        self.UTF8 = 0x08000100
        self.GLOB = int.from_bytes(b"glob", "big")

        cf, ca = self.cf, self.ca
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_int
        cf.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [c_void_p]
        cf.CFArrayGetValueAtIndex.restype = c_void_p
        cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, ctypes.c_long]
        cf.CFArrayCreateMutable.restype = c_void_p
        cf.CFArrayCreateMutable.argtypes = [c_void_p, ctypes.c_long, c_void_p]
        cf.CFDictionaryCreateMutable.restype = c_void_p
        cf.CFDictionaryCreateMutable.argtypes = [c_void_p, ctypes.c_long, c_void_p, c_void_p]
        cf.CFDictionarySetValue.restype = None
        cf.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]
        cf.CFArrayAppendValue.restype = None
        cf.CFArrayAppendValue.argtypes = [c_void_p, c_void_p]
        cf.CFNumberCreate.restype = c_void_p
        cf.CFNumberCreate.argtypes = [c_void_p, ctypes.c_long, c_void_p]

        self._dict_key_cb = c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
        self._dict_val_cb = c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
        self._array_cb = c_void_p.in_dll(cf, "kCFTypeArrayCallBacks")
        self._true = c_void_p.in_dll(cf, "kCFBooleanTrue")

        ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int
        ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, c_void_p, ctypes.c_uint32, c_void_p,
            ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioObjectGetPropertyData.restype = ctypes.c_int
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, c_void_p, ctypes.c_uint32, c_void_p,
            ctypes.POINTER(ctypes.c_uint32), c_void_p]
        ca.AudioObjectSetPropertyData.restype = ctypes.c_int
        ca.AudioObjectSetPropertyData.argtypes = [
            ctypes.c_uint32, c_void_p, ctypes.c_uint32, c_void_p,
            ctypes.c_uint32, c_void_p]
        ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int
        ca.AudioHardwareCreateAggregateDevice.argtypes = [
            c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioHardwareDestroyAggregateDevice.restype = ctypes.c_int
        ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]

    # -- helpers ----------------------------------------------------------
    def cfstr(self, text: str):
        ref = self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), self.UTF8)
        if not ref:
            raise RoutingError("CFStringCreateWithCString failed")
        return ref

    def pystr(self, ref) -> str:
        buf = ctypes.create_string_buffer(2048)
        if not self.cf.CFStringGetCString(ref, buf, 2048, self.UTF8):
            raise RoutingError("CFStringGetCString failed")
        return buf.value.decode("utf-8", "replace")

    def get_data(self, obj: int, selector: int):
        class _Addr(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32),
                        ("scope", ctypes.c_uint32),
                        ("element", ctypes.c_uint32)]

        addr = _Addr(selector, self.GLOB, 0)
        size = ctypes.c_uint32(0)
        err = self.ca.AudioObjectGetPropertyDataSize(
            obj, ctypes.byref(addr), 0, None, ctypes.byref(size))
        if err != 0:
            raise RoutingError("GetPropertyDataSize({:#x}) failed: {}".format(selector, err))
        buf = ctypes.create_string_buffer(size.value)
        n = ctypes.c_uint32(size.value)
        err = self.ca.AudioObjectGetPropertyData(
            obj, ctypes.byref(addr), 0, None, ctypes.byref(n), buf)
        if err != 0:
            raise RoutingError("GetPropertyData({:#x}) failed: {}".format(selector, err))
        return buf, n.value

    def get_ref(self, obj: int, selector: int):
        """Property that returns a CF object reference (8-byte pointer)."""
        buf, n = self.get_data(obj, selector)
        if n != ctypes.sizeof(ctypes.c_void_p):
            raise RoutingError("unexpected property size {} for {:#x}".format(n, selector))
        return ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value

    def get_u32(self, obj: int, selector: int) -> int:
        buf, n = self.get_data(obj, selector)
        if n != 4:
            raise RoutingError("unexpected property size {} for {:#x}".format(n, selector))
        return ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32)).contents.value

    def set_default_output(self, device_id: int) -> None:
        class _Addr(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32),
                        ("scope", ctypes.c_uint32),
                        ("element", ctypes.c_uint32)]

        addr = _Addr(_DEFAULT_OUTPUT, self.GLOB, 0)
        val = ctypes.c_uint32(device_id)
        err = self.ca.AudioObjectSetPropertyData(
            _SYSTEM_OBJECT, ctypes.byref(addr), 0, None, 4, ctypes.byref(val))
        if err != 0:
            raise RoutingError("SetPropertyData(dOut) failed: {}".format(err))

    def set_default_input(self, device_id: int) -> None:
        class _Addr(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32),
                        ("scope", ctypes.c_uint32),
                        ("element", ctypes.c_uint32)]

        addr = _Addr(_DEFAULT_INPUT, self.GLOB, 0)
        val = ctypes.c_uint32(device_id)
        err = self.ca.AudioObjectSetPropertyData(
            _SYSTEM_OBJECT, ctypes.byref(addr), 0, None, 4, ctypes.byref(val))
        if err != 0:
            raise RoutingError("SetPropertyData(dIn) failed: {}".format(err))

    def default_input_id(self) -> int:
        return self.get_u32(_SYSTEM_OBJECT, _DEFAULT_INPUT)

    def set_device_volume(self, device_id: int, percent: float) -> None:
        """Set a device's output volume (0-100). This is the only way to
        control loudness for a Multi-Output Device: macOS routes volume keys
        to the default output, and an aggregate has no master volume."""
        class _Addr(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32),
                        ("scope", ctypes.c_uint32),
                        ("element", ctypes.c_uint32)]

        addr = _Addr(_VOLUME_SCALAR, _OUTPUT_SCOPE, 0)
        scalar = ctypes.c_float(max(0.0, min(1.0, percent / 100.0)))
        err = self.ca.AudioObjectSetPropertyData(
            device_id, ctypes.byref(addr), 0, None,
            ctypes.sizeof(ctypes.c_float), ctypes.byref(scalar))
        if err != 0:
            raise RoutingError("SetPropertyData(volume) failed: {}".format(err))

    def get_device_volume(self, device_id: int) -> float:
        class _Addr(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32),
                        ("scope", ctypes.c_uint32),
                        ("element", ctypes.c_uint32)]

        addr = _Addr(_VOLUME_SCALAR, _OUTPUT_SCOPE, 0)
        size = ctypes.c_uint32(0)
        err = self.ca.AudioObjectGetPropertyDataSize(
            device_id, ctypes.byref(addr), 0, None, ctypes.byref(size))
        if err != 0:
            raise RoutingError("volume is not settable on this device ({})".format(err))
        buf = ctypes.create_string_buffer(size.value)
        n = ctypes.c_uint32(size.value)
        err = self.ca.AudioObjectGetPropertyData(
            device_id, ctypes.byref(addr), 0, None, ctypes.byref(n), buf)
        if err != 0:
            raise RoutingError("GetPropertyData(volume) failed: {}".format(err))
        scalar = ctypes.cast(buf, ctypes.POINTER(ctypes.c_float)).contents.value
        return round(scalar * 100.0)

    def set_device_mute(self, device_id: int, muted: bool) -> None:
        class _Addr(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32),
                        ("scope", ctypes.c_uint32),
                        ("element", ctypes.c_uint32)]

        addr = _Addr(_MUTE, _OUTPUT_SCOPE, 0)
        val = ctypes.c_uint32(1 if muted else 0)
        err = self.ca.AudioObjectSetPropertyData(
            device_id, ctypes.byref(addr), 0, None,
            ctypes.sizeof(ctypes.c_uint32), ctypes.byref(val))
        if err != 0:
            raise RoutingError("SetPropertyData(mute) failed: {}".format(err))

    def get_device_mute(self, device_id: int) -> bool:
        class _Addr(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32),
                        ("scope", ctypes.c_uint32),
                        ("element", ctypes.c_uint32)]

        addr = _Addr(_MUTE, _OUTPUT_SCOPE, 0)
        size = ctypes.c_uint32(0)
        err = self.ca.AudioObjectGetPropertyDataSize(
            device_id, ctypes.byref(addr), 0, None, ctypes.byref(size))
        if err != 0:
            raise RoutingError("mute is not available on this device ({})".format(err))
        buf = ctypes.create_string_buffer(size.value)
        n = ctypes.c_uint32(size.value)
        err = self.ca.AudioObjectGetPropertyData(
            device_id, ctypes.byref(addr), 0, None, ctypes.byref(n), buf)
        if err != 0:
            raise RoutingError("GetPropertyData(mute) failed: {}".format(err))
        return bool(ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32)).contents.value)

    def default_output_id(self) -> int:
        return self.get_u32(_SYSTEM_OBJECT, _DEFAULT_OUTPUT)

    def devices(self) -> List[CaDevice]:
        buf, n = self.get_data(_SYSTEM_OBJECT, _DEVICE_LIST)
        count = n // 4
        ids = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32 * count)).contents
        out = []
        for did in ids:
            try:
                name = self.pystr(self.get_ref(did, _DEVICE_NAME))
                uid = self.pystr(self.get_ref(did, _DEVICE_UID))
            except RoutingError:
                continue
            out.append(CaDevice(did, name, uid))
        return out

    def create_multi_output(self, name: str, sub_uids: List[str],
                            master_uid: str) -> int:
        cf = self.cf
        subs = cf.CFArrayCreateMutable(None, 0, self._array_cb)
        for uid in sub_uids:
            d = cf.CFDictionaryCreateMutable(None, 0, self._dict_key_cb, self._dict_val_cb)
            cf.CFDictionarySetValue(d, self.cfstr("uid"), self.cfstr(uid))
            if uid != master_uid:
                # Slave devices (the loopback) need drift compensation; the
                # master provides the clock for the whole multi-output.
                num = cf.CFNumberCreate(None, 9, ctypes.byref(ctypes.c_int32(1)))
                cf.CFDictionarySetValue(d, self.cfstr("drift compensation"), num)
            cf.CFArrayAppendValue(subs, d)
        desc = cf.CFDictionaryCreateMutable(None, 0, self._dict_key_cb, self._dict_val_cb)
        cf.CFDictionarySetValue(desc, self.cfstr("name"), self.cfstr(name))
        cf.CFDictionarySetValue(desc, self.cfstr("uid"), self.cfstr(name.lower().replace(" ", "-")))
        cf.CFDictionarySetValue(desc, self.cfstr("stacked"), self._true)
        cf.CFDictionarySetValue(desc, self.cfstr("master"), self.cfstr(master_uid))
        cf.CFDictionarySetValue(desc, self.cfstr("subdevices"), subs)
        new_id = ctypes.c_uint32(0)
        err = self.ca.AudioHardwareCreateAggregateDevice(desc, ctypes.byref(new_id))
        if err != 0:
            raise RoutingError("AudioHardwareCreateAggregateDevice failed: {}".format(err))
        return new_id.value

    def destroy_aggregate(self, device_id: int) -> None:
        err = self.ca.AudioHardwareDestroyAggregateDevice(device_id)
        if err != 0:
            raise RoutingError("AudioHardwareDestroyAggregateDevice failed: {}".format(err))


_BACKEND: Optional[_CoreAudio] = None


def backend() -> _CoreAudio:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _CoreAudio()
    return _BACKEND


# ------------------------------------------------------------------ the fix

def load_state(path: Path = STATE_PATH) -> Dict[str, str]:
    """Last successful pairing; empty dict when missing/corrupt."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def save_state(loopback_uid: str, loopback_name: str,
               physical_uid: str, physical_name: str,
               path: Path = STATE_PATH) -> None:
    data = {
        "multi_output_name": MULTI_OUTPUT_NAME,
        "loopback_uid": loopback_uid,
        "loopback_name": loopback_name,
        "physical_uid": physical_uid,
        "physical_name": physical_name,
    }
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # preference is a nicety; the routing itself is already applied


def decide_action(state: Dict[str, str], existing_name: Optional[str],
                  loop_uid: str, phys_uid: str) -> str:
    """Return "reuse" when the existing multi-output is known-good, else "rebuild".

    macOS will not report an aggregate's subdevice list for stacked devices,
    so the persisted pairing (not the HAL) is the source of truth: rebuild
    whenever the state disagrees with what we want or no device exists.
    """
    if existing_name != MULTI_OUTPUT_NAME:
        return "rebuild"
    if state.get("multi_output_name") != MULTI_OUTPUT_NAME:
        return "rebuild"
    if state.get("loopback_uid") != loop_uid or state.get("physical_uid") != phys_uid:
        return "rebuild"
    return "reuse"


def real_outputs(topo: Topology) -> List[AudioDevice]:
    """Real (non-virtual, non-aggregate) output devices, best first."""
    real = [d for d in topo.devices if d.output_channels > 0
            and not d.is_virtual and not d.is_aggregate]
    return sorted(real, key=lambda d: (physical_output_priority(d.name), d.name),
                  reverse=True)


def choose_physical_output(topo: Topology,
                           override: Optional[str] = None,
                           state: Optional[Dict[str, str]] = None) -> Optional[AudioDevice]:
    """Pick the real (non-virtual) output device to pair with the loopback.

    Priority: explicit --output override > remembered preference (when that
    device is still present) > ranked choice. When the remembered device is
    unplugged we fall back to the best available so sound always works, but
    the preference stays stored so it wins again the moment it reappears.
    """
    options = real_outputs(topo)
    if override:
        for dev in options:
            if dev.name == override:
                return dev
        return None
    if not options:
        return None
    if state:
        for dev in options:
            if dev.name == state.get("physical_name"):
                return dev
    return options[0]


def needs_fix(topo: Topology) -> bool:
    """True when the default output is not an aggregate/multi-output."""
    out = topo.device(topo.default_output)
    if out is None:
        return bool(topo.devices) and not topo.system_in_output_path
    return not out.is_aggregate


def restore_routing(topo: Optional[Topology] = None,
                    output: Optional[str] = None,
                    input_device: Optional[str] = None) -> FixResult:
    """Undo everything this tool changed: real default output, real default
    input, and remove the tool-owned Multi-Output Device.

    Used when switching to tap capture (which needs no routing at all) or
    when the user simply wants their Mac back to normal.
    """
    topo = topo or read_system_profiler()
    try:
        ca = backend()
        ca_devices = ca.devices()
    except RoutingError as exc:
        return FixResult(False, False, str(exc))

    out = choose_physical_output(topo, output)
    if out is None or out.name not in {d.name for d in ca_devices}:
        return FixResult(False, False,
                         "Could not find a real output device to restore.\n\n"
                         + walkthrough("BlackHole 2ch", "(your speakers)", MULTI_OUTPUT_NAME))
    out_id = next(d.object_id for d in ca_devices if d.name == out.name)

    mics = topo.mics()
    in_name = None
    if input_device:
        in_name = input_device
    else:
        current = topo.device(topo.default_input)
        if current is not None and current in mics:
            in_name = current.name
        else:
            builtins = [m for m in mics if re.search(r"built-?in|macbook", m.name, re.I)]
            in_name = (builtins or mics or [None])[0].name if (builtins or mics) else None
    if in_name is None or in_name not in {d.name for d in ca_devices}:
        return FixResult(False, False, "Could not find a real microphone to select as "
                                       "the default input{}".format(
                                           " ('{}' not found)".format(input_device)
                                           if input_device else "") + ".")
    in_id = next(d.object_id for d in ca_devices if d.name == in_name)

    removed = False
    try:
        ca.set_default_output(out_id)
        ca.set_default_input(in_id)
        by_name = {d.name: d for d in ca.devices()}
        if MULTI_OUTPUT_NAME in by_name:
            ca.destroy_aggregate(by_name[MULTI_OUTPUT_NAME].object_id)
            removed = True
        try:
            STATE_PATH.unlink(missing_ok=True)
        except OSError:
            pass
    except RoutingError as exc:
        return FixResult(False, False, "macOS refused the restore ({}).".format(exc))

    deadline = time.time() + 3.0
    verified = ""
    while time.time() < deadline and not verified:
        try:
            if ca.default_output_id() == out_id and ca.default_input_id() == in_id:
                verified = " Verified: default output '{}' and default input '{}'.".format(
                    out.name, in_name)
        except RoutingError:
            pass
        time.sleep(0.2)
    return FixResult(True, True,
                     "Default output -> '{}', default input -> '{}'.{}{}"
                     .format(out.name, in_name,
                             " Removed '{}'.".format(MULTI_OUTPUT_NAME) if removed else "",
                             verified or ""))


def _volume_target(ca, ca_devices, topo: Topology,
                   physical_output: Optional[str] = None):
    """The device whose volume the user actually wants to change.

    In loopback mode that is the real output inside the Multi-Output Device
    (the aggregate itself has no volume). Otherwise it is whatever is
    currently the default output -- so the menu, the CLI and remapped volume
    keys behave like the normal system volume in both modes.
    """
    if is_loopback_active(topo):
        physical = choose_physical_output(topo, physical_output, load_state())
        if physical is None:
            return None
        return next((d for d in ca_devices if d.name == physical.name), None)
    default_id = ca.default_output_id()
    for dev in ca_devices:
        if dev.object_id == default_id:
            return dev
    physical = choose_physical_output(topo, physical_output, load_state())
    if physical is None:
        return None
    return next((d for d in ca_devices if d.name == physical.name), None)


def set_output_volume(percent: float, topo: Optional[Topology] = None,
                      physical_output: Optional[str] = None) -> FixResult:
    """Set the audible output device's volume (see _volume_target)."""
    topo = topo or read_system_profiler()
    try:
        ca = backend()
        ca_devices = ca.devices()
    except RoutingError as exc:
        return FixResult(False, False, str(exc))
    target = _volume_target(ca, ca_devices, topo, physical_output)
    if target is None:
        return FixResult(False, False, "No output device found for volume control.")
    try:
        ca.set_device_volume(target.object_id, percent)
        return FixResult(True, True, "'{}' volume -> {:.0f}%".format(
            target.name, clamp_percent(percent)))
    except RoutingError as exc:
        return FixResult(False, False, str(exc))


def get_output_volume(topo: Optional[Topology] = None,
                      physical_output: Optional[str] = None) -> Optional[float]:
    topo = topo or read_system_profiler()
    try:
        ca = backend()
        ca_devices = ca.devices()
    except RoutingError:
        return None
    target = _volume_target(ca, ca_devices, topo, physical_output)
    if target is None:
        return None
    try:
        return ca.get_device_volume(target.object_id)
    except RoutingError:
        return None


def get_output_mute(topo: Optional[Topology] = None,
                    physical_output: Optional[str] = None) -> Optional[bool]:
    topo = topo or read_system_profiler()
    try:
        ca = backend()
        ca_devices = ca.devices()
    except RoutingError:
        return None
    target = _volume_target(ca, ca_devices, topo, physical_output)
    if target is None:
        return None
    try:
        return ca.get_device_mute(target.object_id)
    except RoutingError:
        return None


def set_output_mute(muted: bool, topo: Optional[Topology] = None,
                    physical_output: Optional[str] = None) -> FixResult:
    topo = topo or read_system_profiler()
    try:
        ca = backend()
        ca_devices = ca.devices()
    except RoutingError as exc:
        return FixResult(False, False, str(exc))
    target = _volume_target(ca, ca_devices, topo, physical_output)
    if target is None:
        return FixResult(False, False, "No output device found for mute control.")
    try:
        ca.set_device_mute(target.object_id, muted)
        return FixResult(True, True, "'{}' {}".format(
            target.name, "muted" if muted else "unmuted"))
    except RoutingError as exc:
        return FixResult(False, False, str(exc))


def is_loopback_active(topo: Optional[Topology] = None) -> bool:
    """True when the default output is this tool's Multi-Output Device."""
    topo = topo or read_system_profiler()
    return topo.default_output == MULTI_OUTPUT_NAME


def change_output_volume(delta: float, topo: Optional[Topology] = None,
                         physical_output: Optional[str] = None,
                         loopback_only: bool = False) -> FixResult:
    if loopback_only and not is_loopback_active(topo):
        return FixResult(True, False, "not in loopback mode; leaving volume alone")
    current = get_output_volume(topo, physical_output)
    if current is None:
        return FixResult(False, False, "Could not read the current output volume.")
    target = stepped_volume(current, delta)
    return set_output_volume(target, topo, physical_output)


def deactivate_loopback(topo: Optional[Topology] = None) -> FixResult:
    """Point the default output back at the real device while KEEPING the
    Multi-Output Device and the paired-device state, so hardware volume keys
    work between recordings and the next recording re-selects the device
    instantly (with the same pairing).

    This is the automatic counterpart to restore_routing(), which removes
    the device entirely.
    """
    topo = topo or read_system_profiler()
    try:
        ca = backend()
        ca_devices = ca.devices()
    except RoutingError as exc:
        return FixResult(False, False, str(exc))
    physical = choose_physical_output(topo, None, load_state())
    if physical is None or physical.name not in {d.name for d in ca_devices}:
        return FixResult(False, False, "No real output device found to restore to.")
    dev_id = next(d.object_id for d in ca_devices if d.name == physical.name)
    try:
        ca.set_default_output(dev_id)
    except RoutingError as exc:
        return FixResult(False, False, str(exc))
    return FixResult(True, True, "Default output -> '{}' (loopback device kept)."
                     .format(physical.name))


def list_real_outputs(topo: Optional[Topology] = None) -> List[str]:
    """Names of real output devices, best first (read-only; for menus/CLI)."""
    return [d.name for d in real_outputs(topo or read_system_profiler())]


def fix_routing(topo: Optional[Topology] = None,
                physical_output: Optional[str] = None,
                assume_yes: bool = False) -> FixResult:
    """Create the Multi-Output Device and select it as default output.

    Returns a FixResult; on failure the message contains the click-by-click
    walkthrough so the user is never blocked.
    """
    topo = topo or read_system_profiler()
    loopbacks = sorted(
        (d for d in topo.loopbacks() if system_priority(d.name) >= 70),
        key=lambda d: system_priority(d.name), reverse=True)
    if not loopbacks:
        return FixResult(
            False, False,
            "No general-purpose loopback found. Install BlackHole 2ch first:\n"
            "    brew install blackhole-2ch\n"
            "then re-run this fix.\n\n" + walkthrough("BlackHole 2ch",
                                                      "your speakers/headphones",
                                                      MULTI_OUTPUT_NAME))

    loopback = loopbacks[0]
    state = load_state()
    physical = choose_physical_output(topo, physical_output, state)
    if physical is None:
        return FixResult(
            False, False,
            "Could not identify a real output device (speakers/headphones) to pair "
            "with '{}'.{}\n\n{}".format(
                loopback.name,
                " '{}' was requested but not found.".format(physical_output)
                if physical_output else "",
                walkthrough(loopback.name, "(your speakers/headphones)", MULTI_OUTPUT_NAME)))

    if not physical_output and not state and not assume_yes:
        picked = _prompt_for_output(topo, physical)
        if picked is None:
            return FixResult(False, False, "No output device selected; nothing changed.")
        physical = picked

    try:
        ca = backend()
        ca_devices = ca.devices()
    except RoutingError as exc:
        return FixResult(False, False, "{}\n\n{}".format(exc, _walkthrough_or_empty(loopback, physical)))

    by_name = {d.name: d for d in ca_devices}
    if loopback.name not in by_name or physical.name not in by_name:
        return FixResult(False, False, _walkthrough_or_empty(loopback, physical))

    loop_uid = by_name[loopback.name].uid
    phys_uid = by_name[physical.name].uid
    existing = by_name.get(MULTI_OUTPUT_NAME)
    action = decide_action(state, MULTI_OUTPUT_NAME if existing else None,
                           loop_uid, phys_uid)

    try:
        if action == "rebuild" and existing is not None:
            ca.destroy_aggregate(existing.object_id)
        if action == "rebuild":
            new_id = ca.create_multi_output(MULTI_OUTPUT_NAME, [loop_uid, phys_uid], phys_uid)
            ca.set_default_output(new_id)
            save_state(loop_uid, loopback.name, phys_uid, physical.name)
            return _verified_multi_output(
                ca, new_id, changed=True,
                detail="Rebuilt '{}' ({} + {}) and set it as the default output.".format(
                    MULTI_OUTPUT_NAME, loopback.name, physical.name))
        # Reuse: make sure the multi-output is the system default output.
        ca.set_default_output(existing.object_id)
        return _verified_multi_output(
            ca, existing.object_id, changed=False,
            detail="'{}' ({} + {}) is already correct; selected it as the default output.".format(
                MULTI_OUTPUT_NAME, loopback.name, physical.name))
    except RoutingError as exc:
        return FixResult(False, False, "macOS refused the automatic fix ({}). Any partially "
                                       "created device may still exist in Audio MIDI Setup under "
                                       "'{}'.\n\n{}".format(exc, MULTI_OUTPUT_NAME,
                                                            walkthrough(loopback.name, physical.name, MULTI_OUTPUT_NAME)))


def _prompt_for_output(topo: Topology, default: AudioDevice) -> Optional[AudioDevice]:
    """Interactive pick of the passthrough output (only when stdin is a TTY)."""
    try:
        if not sys.stdin.isatty():
            return default
    except Exception:  # noqa: BLE001
        return default
    options = real_outputs(topo)
    if len(options) <= 1:
        return default
    print("Which output device should play audio alongside the loopback?")
    for i, dev in enumerate(options, 1):
        marker = " (default)" if dev is default else ""
        print("  [{}] {}{}".format(i, dev.name, marker))
    try:
        answer = input("Pick 1-{} [{}]: ".format(len(options), options.index(default) + 1))
    except (EOFError, KeyboardInterrupt):
        return default
    answer = answer.strip()
    if not answer:
        return default
    try:
        return options[int(answer) - 1]
    except (ValueError, IndexError):
        print("Unrecognized choice '{}'; using {}.".format(answer, default.name))
        return default


def _walkthrough_or_empty(loopback: AudioDevice, physical: AudioDevice) -> str:
    try:
        return walkthrough(loopback.name, physical.name, MULTI_OUTPUT_NAME)
    except Exception:  # noqa: BLE001
        return ""


def _verified_multi_output(ca: _CoreAudio, device_id: int, changed: bool,
                           detail: str) -> FixResult:
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            if ca.default_output_id() == device_id:
                return FixResult(True, changed, detail + " Verified: it is the system default output.")
        except RoutingError:
            pass
        time.sleep(0.2)
    return FixResult(True, changed, detail + " (Could not re-read the default output to verify; "
                                          "check System Settings > Sound.)")


# ------------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Fix macOS system-audio routing for loopback capture "
                    "(create a Multi-Output Device and select it as default output).")
    parser.add_argument("--output", default=None, metavar="NAME",
                        help="real output device to include (default: stored preference, "
                             "else ranked choice; prompts interactively when unset)")
    parser.add_argument("--list-outputs", action="store_true",
                        help="list real output devices (pairing candidates) and exit")
    parser.add_argument("--restore-routing", action="store_true",
                        help="undo everything: real default output/input and remove "
                             "the tool-owned Multi-Output Device")
    parser.add_argument("--input", default=None, metavar="NAME",
                        help="with --restore-routing: which microphone to select")
    parser.add_argument("--yes", action="store_true",
                        help="never prompt; accept the chosen/default output")
    parser.add_argument("--volume", type=float, default=None, metavar="PCT",
                        help="set the real output device's volume (0-100) and exit")
    parser.add_argument("--volume-up", action="store_true",
                        help="raise the volume by --step percent and exit")
    parser.add_argument("--volume-down", action="store_true",
                        help="lower the volume by --step percent and exit")
    parser.add_argument("--step", type=float, default=5.0,
                        help="percent per --volume-up/--volume-down (default 5)")
    parser.add_argument("--mute", action="store_true", help="mute the real output")
    parser.add_argument("--unmute", action="store_true", help="unmute the real output")
    parser.add_argument("--toggle-mute", action="store_true",
                        help="flip the real output's mute state")
    parser.add_argument("--loopback-only", action="store_true",
                        help="volume/mute actions only act while the default output is "
                             "the tool's Multi-Output Device (so the native volume keys "
                             "keep behaving normally otherwise)")
    args = parser.parse_args(argv)

    if args.list_outputs:
        state = load_state()
        current = state.get("physical_name")
        for name in list_real_outputs():
            print("{}{}".format(name, "  (paired)" if name == current else ""))
        return 0

    if args.restore_routing:
        result = restore_routing(output=args.output, input_device=args.input)
        print(result.message)
        print("RESULT: {}".format("OK" if result.ok else "MANUAL"))
        return 0 if result.ok else 1

    volume_action = (args.volume is not None or args.volume_up or args.volume_down
                     or args.mute or args.unmute or args.toggle_mute)
    if volume_action:
        if args.loopback_only and not is_loopback_active():
            print("not in loopback mode; leaving volume alone")
            print("RESULT: OK")
            return 0
        if args.volume is not None:
            result = set_output_volume(args.volume, physical_output=args.output)
        elif args.volume_up:
            result = change_output_volume(args.step, physical_output=args.output)
        elif args.volume_down:
            result = change_output_volume(-args.step, physical_output=args.output)
        else:
            muted = get_output_mute(physical_output=args.output)
            if args.toggle_mute and muted is not None:
                result = set_output_mute(not muted, physical_output=args.output)
            else:
                result = set_output_mute(bool(args.mute), physical_output=args.output)
        print(result.message)
        print("RESULT: {}".format("OK" if result.ok else "MANUAL"))
        return 0 if result.ok else 1

    result = fix_routing(physical_output=args.output, assume_yes=args.yes)
    print(result.message)
    print("RESULT: {}".format("OK" if result.ok else "MANUAL"))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
