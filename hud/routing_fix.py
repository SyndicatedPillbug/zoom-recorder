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
import re
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional

try:
    from .devices import AudioDevice, Topology, read_system_profiler, system_priority
except ImportError:  # run as a plain script from the repo root
    from hud.devices import AudioDevice, Topology, read_system_profiler, system_priority  # type: ignore

# The multi-output device this module creates/owns (stable name -> we can
# find and update it on later runs).
MULTI_OUTPUT_NAME = "zoom-recorder Multi-Output"

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
_AGG_SUBDEVICE_LIST = int.from_bytes(b"slst", "big")
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


def choose_physical_output(topo: Topology,
                           override: Optional[str] = None) -> Optional[AudioDevice]:
    """Pick the real (non-virtual) output device to pair with the loopback.

    When the current default output is a loopback-only misroute (e.g. plain
    BlackHole, so nothing is audible), we still want a real device in the
    multi-output, so the choice is made by ranking instead of the default.
    """
    real = [d for d in topo.devices if d.output_channels > 0
            and not d.is_virtual and not d.is_aggregate]
    if override:
        for dev in real:
            if dev.name == override:
                return dev
        return None
    if not real:
        return None
    current = topo.device(topo.default_output)
    if current is not None and current in real:
        return current
    return max(real, key=lambda d: (physical_output_priority(d.name), d.name))


def needs_fix(topo: Topology) -> bool:
    """True when the default output is not an aggregate/multi-output."""
    out = topo.device(topo.default_output)
    if out is None:
        return bool(topo.devices) and not topo.system_in_output_path
    return not out.is_aggregate


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
switch between headphones and speakers, re-run the fix so the multi-output
tracks the device you actually listen on.""".format(loopback=loopback, physical=physical, multi=multi)


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

    def aggregate_subdevice_uids(self, device_id: int) -> Optional[List[str]]:
        try:
            ref = self.get_ref(device_id, _AGG_SUBDEVICE_LIST)
        except RoutingError:
            return None
        uids = []
        count = self.cf.CFArrayGetCount(ref)
        for i in range(count):
            d = self.cf.CFArrayGetValueAtIndex(ref, i)
            uids.append(self.pystr(self.cf.CFDictionaryGetValue(d, self.cfstr("uid"))))
        return uids


_BACKEND: Optional[_CoreAudio] = None


def backend() -> _CoreAudio:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _CoreAudio()
    return _BACKEND


# ------------------------------------------------------------------ the fix

def fix_routing(topo: Optional[Topology] = None,
                physical_output: Optional[str] = None) -> FixResult:
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
    if not needs_fix(topo) and not physical_output:
        return FixResult(True, False,
                         "System audio is routed through '{}' -- nothing to do."
                         .format(topo.default_output))

    physical = choose_physical_output(topo, physical_output)
    if physical is None:
        return FixResult(
            False, False,
            "Could not identify a real output device (speakers/headphones) to pair "
            "with '{}'.{}\n\n{}".format(
                loopback.name,
                " '{}' was requested but not found.".format(physical_output)
                if physical_output else "",
                walkthrough(loopback.name, "(your speakers/headphones)", MULTI_OUTPUT_NAME)))

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
    wanted = {loop_uid, phys_uid}
    existing = by_name.get(MULTI_OUTPUT_NAME)

    try:
        if existing is not None:
            current_uids = ca.aggregate_subdevice_uids(existing.object_id)
            if current_uids is not None and set(current_uids) == wanted:
                ca.set_default_output(existing.object_id)
                return _verified_multi_output(ca, existing.object_id, changed=False,
                                              detail="Existing '{}' is already correct; selected it as the default output.".format(MULTI_OUTPUT_NAME))
            if current_uids is not None and set(current_uids) != wanted:
                ca.destroy_aggregate(existing.object_id)
                existing = None
        if existing is None:
            new_id = ca.create_multi_output(MULTI_OUTPUT_NAME, [loop_uid, phys_uid], phys_uid)
            ca.set_default_output(new_id)
            return _verified_multi_output(
                ca, new_id, changed=True,
                detail="Created '{}' ({} + {}) and set it as the default output.".format(
                    MULTI_OUTPUT_NAME, loopback.name, physical.name))
        # Subdevice list unreadable: trust the existing device.
        ca.set_default_output(existing.object_id)
        return _verified_multi_output(
            ca, existing.object_id, changed=False,
            detail="Selected existing '{}' as the default output.".format(MULTI_OUTPUT_NAME))
    except RoutingError as exc:
        return FixResult(False, False, "macOS refused the automatic fix ({}).{}\n\n{}".format(
            exc, _reused_or_destroyed_note(loopback, physical),
            walkthrough(loopback.name, physical.name, MULTI_OUTPUT_NAME)))


def _reused_or_destroyed_note(loopback: AudioDevice, physical: AudioDevice) -> str:
    return (" Any partially created device may still exist in Audio MIDI Setup under "
            "'{}'.".format(MULTI_OUTPUT_NAME))


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
                        help="real output device to include (default: ranked choice)")
    args = parser.parse_args(argv)
    result = fix_routing(physical_output=args.output)
    print(result.message)
    print("RESULT: {}".format("OK" if result.ok else "MANUAL"))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
