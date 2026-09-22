#!/usr/bin/env python3
"""Environment doctor: `./zoom_record.py --doctor`.

Answers "will this work on this machine, and if not, what exactly do I do?"
without requesting any permission the tool does not need. It checks the
external tools, the menu-bar dependency, the BlackHole loopback, output
volume control, the microphone (a short, silent probe) and the routing, and
prints actionable fixes for anything missing.

Deliberately absent: any Screen Recording / Audio Capture request. The normal
capture path (loopback) needs no such grant; the optional Core Audio tap path
is only used when explicitly requested.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

try:
    from .devices import read_system_profiler, system_priority
except ImportError:  # plain script / -m from the repo root
    from hud.devices import read_system_profiler, system_priority  # type: ignore

MICROPHONE_SETTINGS_URL = ("x-apple.systempreferences:com.apple.preference."
                           "security?Privacy_Microphone")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    critical: bool = True


def check_macos() -> Check:
    version = platform.mac_ver()[0] or "unknown"
    try:
        major = int(version.split(".")[0])
    except (ValueError, IndexError):
        major = 0
    return Check("macOS", major >= 13, version,
                 "zoom-recorder needs macOS 13 or newer.")


def check_python() -> Check:
    version = platform.python_version()
    ok = tuple(int(x) for x in version.split(".")[:2]) >= (3, 9)
    return Check("python", ok, version, "Install Python 3.9+ (system python3 is fine).")


def check_tools() -> List[Check]:
    checks = []
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        checks.append(Check(tool, bool(path), path or "not found",
                            "brew install ffmpeg"))
    return checks


def check_rumps() -> Check:
    try:
        import rumps  # noqa: F401
        return Check("rumps (menu bar)", True, "installed")
    except ImportError:
        return Check("rumps (menu bar)", False, "not installed",
                     "pip3 install --user rumps", critical=False)


def check_blackhole() -> Check:
    topo = read_system_profiler()
    good = [d.name for d in topo.loopbacks() if system_priority(d.name) >= 70]
    if good:
        return Check("BlackHole loopback", True, good[0])
    return Check("BlackHole loopback", False, "no general-purpose loopback found",
                 "brew install blackhole-2ch  (asks for your admin password)")


def check_routing() -> Check:
    """Capability check, not a snapshot of the current default.

    The normal between-recordings state is the real device (so hardware
    volume keys work); the recorder switches to the Multi-Output Device while
    recording and hands the real device back afterwards. Only a *bare*
    loopback as the default output is a real problem (captured but inaudible).
    """
    topo = read_system_profiler()
    out = topo.device(topo.default_output)
    if out is not None and out.is_aggregate:
        return Check("audio routing", True,
                     "recording setup is active ({})".format(out.name))
    if out is not None and out.is_loopback and not out.is_aggregate:
        return Check("audio routing", False, out.name,
                     "Run: ./zoom_record.py --fix-routing", critical=False)
    good = [d.name for d in topo.loopbacks() if system_priority(d.name) >= 70]
    if not good:
        return Check("audio routing", False, "no loopback available",
                     "brew install blackhole-2ch", critical=False)
    return Check("audio routing", True,
                 "ready (sound plays through {}; switches automatically while "
                 "recording)".format(topo.default_output or "your output"))


def check_output_volume() -> Check:
    try:
        from .routing_fix import get_output_volume
    except ImportError:
        from hud.routing_fix import get_output_volume  # type: ignore
    try:
        volume = get_output_volume()
    except Exception as exc:  # noqa: BLE001
        return Check("output volume control", False, str(exc), critical=False)
    if volume is None:
        return Check("output volume control", False, "no controllable output device",
                     critical=False)
    return Check("output volume control", True, "{:.0f}%".format(volume))


def check_microphone(seconds: float = 1.5) -> Check:
    """Short silent probe of a *real* microphone (loopbacks excluded); the
    recorder selects one automatically, so this just proves the machine (and
    the calling app context) can open one and get signal."""
    try:
        from zoom_record import list_devices, probe_level
    except Exception as exc:  # noqa: BLE001
        return Check("microphone", False, "probe unavailable: {}".format(exc),
                     critical=False)
    inputs, _outputs = list_devices()
    if not inputs:
        return Check("microphone", False, "no input devices found")
    topo = read_system_profiler()
    real = [d for d in inputs if topo.looks_like_mic(d.name)]
    if not real:
        return Check("microphone", False, "no real microphone found (only loopbacks)")
    default = topo.device(topo.default_input)
    device = next((d for d in real if default is not None and d.name == default.name),
                  real[0])
    probe = probe_level(device, seconds, max_wait=seconds + 2.5)
    if not probe.ok:
        return Check("microphone", False,
                     "{}: {}".format(device.name, probe.error or "could not open"),
                     "Grant Microphone access: System Settings > Privacy & "
                     "Security > Microphone (run: open \"{}\")".format(
                         MICROPHONE_SETTINGS_URL))
    if probe.max_db is not None and probe.max_db < -80.0:
        return Check("microphone", False,
                     "{}: digital silence ({:.1f} dB)".format(device.name, probe.max_db),
                     "The microphone opened but produced no signal. Check "
                     "System Settings > Privacy & Security > Microphone (run: "
                     "open \"{}\") and that the mic is not muted.".format(
                         MICROPHONE_SETTINGS_URL),
                     critical=False)
    level = "{:.1f} dB".format(probe.max_db) if probe.max_db is not None else "silent"
    return Check("microphone", True, "{} ({})".format(device.name, level))


def check_transcription() -> Check:
    """Local transcription readiness (whisper-server + a model file)."""
    try:
        from .config import recorder_defaults
    except ImportError:
        from hud.config import recorder_defaults  # type: ignore
    server = shutil.which("whisper-server") or shutil.which("whisper-cli")
    model = Path(recorder_defaults().transcription_model).expanduser()
    if not server:
        return Check("transcription (local)", False, "whisper.cpp not installed",
                     "brew install whisper.cpp", critical=False)
    if not model.is_file():
        return Check("transcription (local)", False,
                     "model not downloaded ({})".format(model.name),
                     "Download it from the Setup tab (about 150 MB)",
                     critical=False)
    return Check("transcription (local)", True,
                 "{} + {}".format(Path(server).name, model.name))


def check_tap() -> Check:
    try:
        from .system_tap import available, usable_in_this_context
    except ImportError:
        from hud.system_tap import available, usable_in_this_context  # type: ignore
    if not available():
        return Check("system tap (optional)", True,
                     "not available on this macOS", critical=False)
    if usable_in_this_context():
        return Check("system tap (optional)", True,
                     "available in this context (--system-capture tap)",
                     critical=False)
    return Check("system tap (optional)", True,
                 "available, but this app context cannot be granted capture; "
                 "loopback capture is used instead", critical=False)


def run_doctor(probe_seconds: float = 1.5) -> bool:
    checks: List[Check] = [check_macos(), check_python()]
    checks += check_tools()
    checks += [check_rumps(), check_blackhole(), check_routing(),
               check_output_volume(), check_microphone(probe_seconds),
               check_transcription(), check_tap()]

    print("zoom-recorder doctor")
    print("====================")
    for check in checks:
        mark = "ok" if check.ok else ("!!" if check.critical else " -")
        print("  [{}] {:<26} {}".format(mark, check.name, check.detail))
        if not check.ok and check.fix:
            print("       fix: {}".format(check.fix))
    print("")
    print("This tool records your microphone and, via the BlackHole loopback, "
          "the other party's audio.")
    print("It never requests Screen Recording and sends nothing off the machine "
          "unless you enable --live.")
    problems = [c for c in checks if not c.ok and c.critical]
    print("RESULT: {}".format("OK" if not problems else
                              "PROBLEMS ({})".format(len(problems))))
    return not problems


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Check the zoom-recorder environment.")
    parser.add_argument("--probe-seconds", type=float, default=1.5)
    args = parser.parse_args()
    return 0 if run_doctor(args.probe_seconds) else 1


if __name__ == "__main__":
    raise SystemExit(main())
