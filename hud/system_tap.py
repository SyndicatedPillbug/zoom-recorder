#!/usr/bin/env python3
"""System-audio capture via macOS Core Audio process taps (macOS 14.2+).

This is the modern replacement for the BlackHole-in-a-Multi-Output-Device
approach: the system default output stays untouched (so volume keys, Sound
Settings and per-app output choices all behave normally), while the
recorder receives a copy of everything that plays. The tap is created with
``CATapUnmuted`` (observe-only: audio still reaches your ears) and the tap
sees the audio *before* the output volume fader, so the recording level is
independent of how loud the user has set playback.

How it works (all public APIs):

  1. ``CATapDescription`` (ObjC, via PyObjC) describes a private, unmuted
     global tap: a stereo mixdown of every playing process.
  2. ``AudioHardwareCreateProcessTap`` creates it.
  3. A *private* HAL aggregate device whose tap list contains the tap
     (with drift compensation, per Apple's guidance) turns the tap into a
     readable input. It never shows up in Sound settings.
  4. ``AudioDeviceCreateIOProcIDWithBlock`` installs the callback that
     pulls float32 PCM; a feeder thread writes it into a pipe that ffmpeg
     reads as a normal input (``-f f32le -i pipe:N``).

Threading rules honored here (per Apple's guidance): the IO callback runs
on the HAL's real-time thread, so it only copies into a bounded FIFO; the
feeder thread does the pipe writes with drop-oldest-on-stall so the IO
thread never blocks.

Permission: the first time a process starts IO on a tap aggregate, macOS
prompts once for **System Audio Recording** access (System Settings >
Privacy & Security > Screen & System Audio Recording). If it is denied,
the tap keeps flowing but delivers silence -- ``system_tap_self_test``
detects that and points at the switch.

The ObjC block is built with raw ctypes (the block literal ABI is stable):
an ISA pointer to _NSConcreteStackBlock, BLOCK_IS_GLOBAL, and an invoke
function pointer.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional

try:
    from .routing_fix import backend
except ImportError:  # run as a plain script from the repo root
    from hud.routing_fix import backend  # type: ignore

TAP_NAME = "zoom-recorder system tap"
AGGREGATE_UID = "zoom-recorder-system-tap-capture"
# The name the recorder reports for the tap source (HUD pills, logs).
SOURCE_NAME = "System Tap (macOS)"

_TAP_FORMAT = int.from_bytes(b"tfmt", "big")  # kAudioTapPropertyFormat
_BLOCK_IS_GLOBAL = 1 << 28
_MIN_MACOS = (14, 2)


class TapError(Exception):
    pass


def _macos_version() -> tuple:
    try:
        return tuple(int(x) for x in platform.mac_ver()[0].split(".")[:2])
    except (ValueError, IndexError):
        return (0, 0)


def available() -> bool:
    """True when this machine supports process taps (macOS >= 14.2)."""
    if platform.system() != "Darwin":
        return False
    if _macos_version() < _MIN_MACOS:
        return False
    try:
        return hasattr(_backend_ca(), "AudioHardwareCreateProcessTap")
    except OSError:
        return False


def in_granted_terminal() -> bool:
    """True when running under Apple Terminal.

    macOS attributes audio-capture permission to the responsible app: a
    process spawned from Terminal inherits Terminal's grants, while a
    launchd/GUI-spawned process (the menu bar) is attributed to its own
    context, where the System Audio Recording grant cannot be created on
    macOS 15 (the tap starts but delivers silence and wedges mic opens)."""
    return os.environ.get("TERM_PROGRAM") == "Apple_Terminal"


def usable_in_this_context() -> bool:
    """True when tap capture is expected to actually work right here.

    Both the recorder (mode selection) and the menu bar (whether to offer
    the loopback volume slider) must agree on this; capability alone is not
    enough -- a machine can support taps while this process context cannot
    be granted capture permission."""
    return available() and in_granted_terminal()


def _backend_ca():
    return backend().ca


@dataclass
class TapFormat:
    sample_rate: int
    channels: int
    bits: int
    is_float: bool


def _read_tap_format(ca, tap_id: int) -> TapFormat:
    c_void_p = ctypes.c_void_p

    class _Addr(ctypes.Structure):
        _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                    ("element", ctypes.c_uint32)]

    addr = _Addr(_TAP_FORMAT, 0x676C6F62, 0)  # 'glob'
    size = ctypes.c_uint32(0)
    err = ca.AudioObjectGetPropertyDataSize(tap_id, ctypes.byref(addr), 0,
                                            None, ctypes.byref(size))
    if err != 0:
        raise TapError("kAudioTapPropertyFormat failed: {}".format(err))
    buf = ctypes.create_string_buffer(size.value)
    n = ctypes.c_uint32(size.value)
    err = ca.AudioObjectGetPropertyData(tap_id, ctypes.byref(addr), 0,
                                        None, ctypes.byref(n), buf)
    if err != 0:
        raise TapError("kAudioTapPropertyFormat failed: {}".format(err))
    rate, fmt, flags, _bpp, _fpp, _bpf, chans, bits, _res = struct.unpack_from(
        "<dIIIIIIII", buf.raw[:size.value])
    lpcm = struct.pack(">I", fmt) == b"lpcm"
    is_float = lpcm and bool(flags & 0x1)  # kAudioFormatFlagIsFloat
    return TapFormat(sample_rate=int(rate), channels=chans, bits=bits,
                     is_float=is_float)


class _IOCallback:
    """The ctypes-level plumbing for the ObjC block the HAL calls."""

    def __init__(self, sink: Callable[[bytes], None]) -> None:
        c_void_p = ctypes.c_void_p

        class _AudioBuffer(ctypes.Structure):
            _fields_ = [("mNumberChannels", ctypes.c_uint32),
                        ("mDataByteSize", ctypes.c_uint32),
                        ("mData", c_void_p)]

        class _AudioBufferList(ctypes.Structure):
            _fields_ = [("mNumberBuffers", ctypes.c_uint32),
                        ("mBuffers", _AudioBuffer * 1)]

        class _BlockDescriptor(ctypes.Structure):
            _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_size_t)]

        class _BlockLiteral(ctypes.Structure):
            _fields_ = [("isa", c_void_p), ("flags", ctypes.c_int),
                        ("reserved", ctypes.c_int), ("invoke", c_void_p),
                        ("descriptor", c_void_p)]

        def ioinvoke(block_ptr, now, inb, intime, outb, outtime):
            if inb:
                try:
                    lst = ctypes.cast(inb, ctypes.POINTER(_AudioBufferList)).contents
                    for i in range(lst.mNumberBuffers):
                        b = lst.mBuffers[i]
                        if b.mDataByteSize and b.mData:
                            sink(ctypes.string_at(b.mData, b.mDataByteSize))
                except Exception:  # noqa: BLE001 - never raise into the HAL
                    pass

        self._invoke = ctypes.CFUNCTYPE(
            None, c_void_p, c_void_p, c_void_p, c_void_p, c_void_p, c_void_p)(ioinvoke)
        lib = ctypes.CDLL(ctypes.util.find_library("libSystem"))
        self._descriptor = _BlockDescriptor(0, ctypes.sizeof(_BlockLiteral))
        self._literal = _BlockLiteral()
        self._literal.isa = ctypes.c_void_p.in_dll(lib, "_NSConcreteStackBlock")
        self._literal.flags = _BLOCK_IS_GLOBAL
        self._literal.invoke = ctypes.cast(self._invoke, c_void_p)
        self._literal.descriptor = ctypes.cast(ctypes.byref(self._descriptor), c_void_p)


class SystemTap:
    """A live system-audio tap feeding PCM through a pipe.

    Usage:
        tap = SystemTap()
        tap.start()
        fd = tap.read_fd        # ffmpeg: -f f32le -ar R -ac C -i pipe:fd
        ...
        tap.stop()              # destroys tap + aggregate (never leaks)
    """

    def __init__(self, log: Optional[Callable[[str], None]] = None) -> None:
        self.log = log or (lambda _msg: None)
        self._tap_id: Optional[int] = None
        self._agg_id: Optional[int] = None
        self._ioproc_id: Optional[int] = None
        self._io = None
        self.read_fd: Optional[int] = None
        self.write_fd: Optional[int] = None
        self.format = None
        self._io_deferred = False
        self.bytes_total = 0
        self.bytes_dropped = 0
        self._last_bytes_at = 0.0
        self._started_at = 0.0
        # Level tracking (feeder thread): used to tell "permission denied"
        # (buffers flow, all zeros) from a stalled tap (no callbacks at all).
        self._silent_bytes = 0
        self._last_loud_at = 0.0
        self._fifo: deque = deque()
        self._fifo_lock = threading.Lock()
        self._feeder_stop = threading.Event()
        self._feeder: Optional[threading.Thread] = None
        self._started = False

    # -- lifecycle ---------------------------------------------------------
    def start(self, io_deferred: bool = False) -> None:
        """Create the tap + private aggregate + IOProc.

        With ``io_deferred`` (recommended), audio IO is NOT started here:
        call :meth:`start_io` once the other capture inputs are confirmed
        healthy. Starting a global tap's IO while the avfoundation mic open
        is in flight can wedge the mic device open (observed on macOS
        15.7), leaving every subsequent mic open hanging.
        """
        if self._started:
            raise TapError("tap already started")
        import objc
        ca = _backend_ca()
        c_void_p = ctypes.c_void_p
        CATapDescription = objc.lookUpClass("CATapDescription")
        if CATapDescription is None:
            raise TapError("CATapDescription not available on this macOS")

        desc = CATapDescription.alloc().initStereoGlobalTapButExcludeProcesses_([])
        desc.setName_(TAP_NAME)
        desc.setPrivate_(True)
        desc.setMuteBehavior_(0)  # CATapUnmuted: capture without silencing
        tap_uuid = str(desc.UUID().UUIDString())

        tap_id = ctypes.c_uint32(0)
        err = ca.AudioHardwareCreateProcessTap(
            c_void_p(objc.pyobjc_id(desc)), ctypes.byref(tap_id))
        if err != 0:
            raise TapError("AudioHardwareCreateProcessTap failed: {}".format(err))
        self._tap_id = tap_id.value
        self.format = _read_tap_format(ca, self._tap_id)

        agg_id = ctypes.c_uint32(0)
        err = ca.AudioHardwareCreateAggregateDevice(
            self._aggregate_desc(tap_uuid), ctypes.byref(agg_id))
        if err != 0:
            self._destroy_tap()
            raise TapError("AudioHardwareCreateAggregateDevice failed: {}".format(err))
        self._agg_id = agg_id.value

        read_fd, write_fd = os.pipe()
        self.read_fd, self.write_fd = read_fd, write_fd

        self._io = _IOCallback(self._enqueue)
        ioproc_id = c_void_p(0)
        err = ca.AudioDeviceCreateIOProcIDWithBlock(
            ctypes.byref(ioproc_id), self._agg_id, None,
            ctypes.byref(self._io._literal))
        if err != 0:
            self._close_pipe()
            self._destroy_all()
            raise TapError("AudioDeviceCreateIOProcIDWithBlock failed: {}".format(err))
        self._ioproc_id = ioproc_id.value

        self._io_deferred = io_deferred
        if not io_deferred:
            self.start_io()

    def start_io(self) -> None:
        """Begin audio IO (deferred mode). Safe to call once."""
        if self._started:
            raise TapError("tap already started")
        if self._agg_id is None or self._ioproc_id is None:
            raise TapError("tap not prepared; call start() first")
        ca = _backend_ca()
        err = ca.AudioDeviceStart(self._agg_id, self._ioproc_id)
        if err != 0:
            self._destroy_all()
            raise TapError("AudioDeviceStart failed: {}".format(err))

        self._feeder = threading.Thread(
            target=self._feeder_loop, name="zoom-tap-feeder", daemon=True)
        self._feeder.start()
        self._started = True
        self._started_at = time.time()
        self._last_loud_at = time.time()
        self.log("System tap started: {} Hz, {} ch, {}{}".format(
            self.format.sample_rate, self.format.channels,
            "float" if self.format.is_float else "int", self.format.bits))

    def stop(self) -> None:
        if not self._started:
            # Deferred and never started: still tear down whatever exists.
            self._destroy_all()
            return
        self._feeder_stop.set()
        try:
            if self._feeder is not None:
                self._feeder.join(timeout=2.0)
        except RuntimeError:
            pass
        ca = _backend_ca()
        try:
            if self._agg_id is not None and self._ioproc_id is not None:
                ca.AudioDeviceStop(self._agg_id, self._ioproc_id)
        except Exception:  # noqa: BLE001
            pass
        self._destroy_all()
        self._started = False

    # -- plumbing ----------------------------------------------------------
    @staticmethod
    def _aggregate_desc(tap_uuid: str):
        helpers = backend()
        cf, c_void_p = helpers.cf, ctypes.c_void_p
        KDK = c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
        KDV = c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
        KAC = c_void_p.in_dll(cf, "kCFTypeArrayCallBacks")
        true = c_void_p.in_dll(cf, "kCFBooleanTrue")

        adesc = cf.CFDictionaryCreateMutable(None, 0, KDK, KDV)
        cf.CFDictionarySetValue(adesc, helpers.cfstr("name"), helpers.cfstr(TAP_NAME))
        cf.CFDictionarySetValue(adesc, helpers.cfstr("uid"), helpers.cfstr(AGGREGATE_UID))
        cf.CFDictionarySetValue(adesc, helpers.cfstr("private"), true)
        taps = cf.CFArrayCreateMutable(None, 0, KAC)
        entry = cf.CFDictionaryCreateMutable(None, 0, KDK, KDV)
        cf.CFDictionarySetValue(entry, helpers.cfstr("uid"), helpers.cfstr(tap_uuid))
        cf.CFDictionarySetValue(entry, helpers.cfstr("drift"), true)
        cf.CFArrayAppendValue(taps, entry)
        cf.CFDictionarySetValue(adesc, helpers.cfstr("taps"), taps)
        num = cf.CFNumberCreate(None, 9, ctypes.byref(ctypes.c_int32(1)))
        cf.CFDictionarySetValue(adesc, helpers.cfstr("tapautostart"), num)
        return adesc

    def _enqueue(self, chunk: bytes) -> None:
        self.bytes_total += len(chunk)
        self._last_bytes_at = time.time()
        with self._fifo_lock:
            self._fifo.append(chunk)
            # Hard cap: if the reader stalls, drop the oldest so the HAL's
            # real-time IO thread is never blocked.
            while len(self._fifo) > 64:
                self._fifo.popleft()
                self.bytes_dropped += 1

    def _feeder_loop(self) -> None:
        os.set_blocking(self.write_fd, False)
        pending = bytearray()
        running = True
        while running or pending:
            if running and not self._feeder_stop.is_set():
                self._fifo_lock.acquire()
                got = False
                while self._fifo:
                    pending.extend(self._fifo.popleft())
                    got = True
                self._fifo_lock.release()
                if not got:
                    time.sleep(0.02)
            else:
                # Stopping: drain whatever remains, then finish.
                self._fifo_lock.acquire()
                while self._fifo:
                    pending.extend(self._fifo.popleft())
                self._fifo_lock.release()
                running = False
            while pending:
                chunk = pending[:65536]
                self._track_level(bytes(chunk))
                try:
                    written = os.write(self.write_fd, chunk)
                    del pending[:written]
                except OSError as exc:
                    if exc.errno == errno.EPIPE:
                        return  # reader gone (ffmpeg exited); stop feeding
                    if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    time.sleep(0.02)  # pipe full: let the reader drain
                    break
        try:
            os.close(self.write_fd)
        except OSError:
            pass
        self.write_fd = None

    def _track_level(self, chunk: bytes) -> None:
        """Feeder-thread level accounting: peaks of float32 PCM."""
        n = len(chunk) // 4
        if not n:
            return
        fmt = "<{}f".format(n)
        samples = struct.unpack_from(fmt, chunk)
        peak = max(abs(s) for s in samples)
        if peak > 1e-5:
            self._last_loud_at = time.time()
        else:
            self._silent_bytes += len(chunk)

    def stalled_after(self, seconds: float = 3.0) -> bool:
        """True when IO callbacks stopped delivering (hiccup/route change)."""
        if not self._started or self.bytes_total == 0:
            return False
        return time.time() - self._last_bytes_at > seconds

    def running_seconds(self) -> float:
        if not self._started:
            return 0.0
        return time.time() - self._started_at

    def silent_seconds(self) -> float:
        """Seconds since the tap last delivered a non-zero sample.

        For a permission-denied tap the buffers keep flowing but carry only
        zeros -- distinct from a stalled tap (no callbacks at all)."""
        if not self._started:
            return 0.0
        return time.time() - self._last_loud_at

    def health_line(self) -> str:
        if not self._started:
            return "not started"
        if self.bytes_total == 0:
            return "{:.0f}s in, no data yet".format(self.running_seconds())
        silent = self.silent_seconds()
        return "{:.1f} MB captured, {} ({:.0f}s all-silence)".format(
            self.bytes_total / 1e6,
            "SILENT" if silent > 5.0 else "audio flowing",
            silent)

    # -- teardown ----------------------------------------------------------
    def _close_pipe(self) -> None:
        if self.write_fd is not None:
            try:
                os.close(self.write_fd)
            except OSError:
                pass
            self.write_fd = None
        if self.read_fd is not None:
            try:
                os.close(self.read_fd)
            except OSError:
                pass
            self.read_fd = None

    def _destroy_tap(self) -> None:
        if self._tap_id is not None:
            _backend_ca().AudioHardwareDestroyProcessTap(self._tap_id)
            self._tap_id = None

    def _destroy_all(self) -> None:
        ca = _backend_ca()
        if self._agg_id is not None:
            if self._ioproc_id is not None:
                ca.AudioDeviceDestroyIOProcID(self._agg_id, self._ioproc_id)
                self._ioproc_id = None
            ca.AudioHardwareDestroyAggregateDevice(self._agg_id)
            self._agg_id = None
        self._destroy_tap()
        self._close_pipe()

    # -- ffmpeg integration -------------------------------------------------
    def ffmpeg_args(self) -> List[str]:
        f = self.format
        return ["-thread_queue_size", "4096",
                "-f", "f32le" if f.is_float else "s16le",
                "-ar", str(f.sample_rate), "-ac", str(f.channels),
                "-i", "pipe:{}".format(self.read_fd)]

    def name(self) -> str:
        return SOURCE_NAME


def system_tap_self_test(probe_seconds: float = 4.0,
                         log: Optional[Callable[[str], None]] = None) -> bool:
    """Play a tone and confirm the tap receives it.

    Returns True on success; on failure the message explains the likely
    fix (normally the one-time System Audio Recording permission).
    """
    say = log or (lambda m: None)
    import select
    import subprocess

    tap = SystemTap(log=say)
    tap.start()
    try:
        tmp = "/tmp/zoomtap_tone.wav"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi",
                        "-i", "sine=frequency=1000:duration={:.1f}".format(probe_seconds),
                        "-ar", "48000", "-ac", "2", tmp],
                       capture_output=True, check=True)
        say("Playing a 1 kHz tone on the default output for {:.1f}s...".format(probe_seconds))
        player = subprocess.Popen(["afplay", tmp],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        player.wait()

        data = bytearray()
        deadline = time.time() + 3.0
        os.set_blocking(tap.read_fd, False)
        while time.time() < deadline and len(data) < 4_000_000:
            r, _, _ = select.select([tap.read_fd], [], [], 0.2)
            if r:
                chunk = os.read(tap.read_fd, 65536)
                if chunk:
                    data.extend(chunk)
                else:
                    break  # EOF: feeder closed the pipe
        tap.stop()
        n = len(data) // 4
        if not n:
            say("System tap FAILED: no audio flowed from the tap.")
            return False
        samples = struct.unpack_from("<{}f".format(n), bytes(data[:n * 4]))
        peak = max(abs(s) for s in samples)
        if peak < 1e-5:
            say("System tap FAILED: buffers flow but carry silence.")
            say("  Grant System Audio Recording access: System Settings > "
                "Privacy & Security > Screen & System Audio Recording, then retry.")
            return False
        say("System tap PASSED: tone captured (peak {:.3f}).".format(peak))
        return True
    except Exception as exc:  # noqa: BLE001
        try:
            tap.stop()
        except Exception:  # noqa: BLE001
            pass
        say("System tap FAILED: {}".format(exc))
        return False


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Verify macOS system-audio capture via a Core Audio tap.")
    parser.add_argument("--probe-seconds", type=float, default=4.0)
    parser.add_argument("--open-settings", action="store_true",
                        help="open the Screen & System Audio Recording privacy pane")
    args = parser.parse_args(argv)
    if args.open_settings:
        import subprocess
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference."
                                 "security?Privacy_ScreenCapture"])
        return 0
    if not available():
        print("System taps need macOS 14.2+ (this is macOS {}).".format(
            platform.mac_ver()[0] or "unknown"))
        return 2
    ok = system_tap_self_test(args.probe_seconds, log=print)
    print("RESULT: {}".format("OK" if ok else "MANUAL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
