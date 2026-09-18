#!/usr/bin/env python3
"""One-keystroke-equivalent activation: a menu-bar toggle for zoom_record.py.

No background daemon watches for calls or auto-arms itself -- per the
2026-09-16 decision, a persistent process with silent mic access and file
writes is exactly what an EDR (Falcon) is tuned to flag, and it's a consent
risk if it ever records something it shouldn't. This app only exists, visibly,
in the menu bar; the recorder subprocess only exists while you're actually
recording, and the icon always shows which state you're in (🎙 idle,
🔴 recording, 🧠 HUD recording with the live window up).

The menu gives the live HUD its own distinct options: "Start with Live HUD"
and "Open Live HUD…", separate from the plain "Start Recording" toggle.

Usage:
    pip3 install --user rumps
    ./menubar.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import rumps

from hud.menu_state import describe

SCRIPT_DIR = Path(__file__).resolve().parent
RECORDER = SCRIPT_DIR / "zoom_record.py"
SETTINGS = SCRIPT_DIR / "settings.py"
PIDFILE = Path.home() / ".zoom_recorder.pid"
HUD_URLFILE = Path.home() / ".zoom_recorder_hud.url"
SETTINGS_PIDFILE = Path.home() / ".zoom_recorder_settings.pid"
SETTINGS_URLFILE = Path.home() / ".zoom_recorder_settings.url"

IDLE_TITLE = "🎙"


def audio_menu_specs(outputs, paired):
    """Pure spec for the "Audio Out" dropdown: [(label, device_name_or_None)].

    label None means a separator; device_name None means "Rebuild Routing".
    """
    specs = [(("✓ " if name == paired else "") + name, name) for name in outputs]
    specs.append((None, None))
    specs.append(("Rebuild Routing", None))
    return specs


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pidfile(path: Path = PIDFILE) -> "int | None":
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        path.unlink(missing_ok=True)
        return None
    if not _pid_alive(pid):
        path.unlink(missing_ok=True)
        return None
    return pid


class RecorderApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(IDLE_TITLE, quit_button="Quit")
        self.toggle_item = rumps.MenuItem("Start Recording", callback=self.toggle)
        self.live_item = rumps.MenuItem("Start with Live HUD", callback=self.start_live)
        self.open_hud_item = rumps.MenuItem("Open Live HUD…", callback=self.open_hud)
        self.settings_item = rumps.MenuItem("Settings…", callback=self.open_settings)
        self.audio_item = rumps.MenuItem("Audio Out")
        self.menu = [self.toggle_item, self.live_item, None,
                     self.open_hud_item, self.settings_item, self.audio_item]
        self._audio_sig = None
        self._sync_ui()

    def _hud_active(self) -> bool:
        # The HUD writes its URL file on start and removes it on stop, so its
        # presence alongside a live recorder means the HUD is actually up.
        return HUD_URLFILE.is_file() and _read_pidfile() is not None

    def _sync_ui(self) -> None:
        recording = _read_pidfile() is not None
        state = describe(recording, self._hud_active())
        self.title = state["icon"]
        self.toggle_item.title = state["toggle_title"]
        self.live_item.title = state["live_title"]
        self.live_item.set_callback(self.start_live if state["live_enabled"] else None)
        self.open_hud_item.set_callback(self.open_hud if state["open_enabled"] else None)
        self._sync_audio_menu()

    def _sync_audio_menu(self) -> None:
        # Rebuild the "Audio Out" submenu only when the device set actually
        # changed (this runs on the 5s poll; CoreAudio reads are cheap but
        # rebuilding menu items every tick would reset their callbacks).
        try:
            from hud.system_tap import available as tap_available
            from hud.routing_fix import list_real_outputs, load_state, get_output_volume
            tap_ok = tap_available()
            outputs = [] if tap_ok else list_real_outputs()
            paired = None if tap_ok else load_state().get("physical_name")
            volume = None if tap_ok else get_output_volume()
        except Exception:
            tap_ok = True  # conservative: assume nothing to switch
            outputs, paired, volume = [], None, None
        sig = (tuple(outputs), paired, tap_ok)
        if sig == self._audio_sig:
            return
        self._audio_sig = sig
        if tap_ok:
            # Tap capture needs no routing changes at all: the tap follows
            # whatever the default output is. Only offer the undo switch.
            self.audio_item.menu = [
                rumps.MenuItem("(direct capture: routing untouched)", callback=None),
                rumps.MenuItem("Restore Normal Routing…", callback=self.restore_routing),
            ]
            return
        # Loopback (Multi-Output) mode: volume keys do not work for it, so
        # provide the volume control macOS omits.
        items = []
        for label, name in audio_menu_specs(outputs, paired):
            if label is None:
                items.append(None)
            elif name is None:
                items.append(rumps.MenuItem(label, callback=self.fix_routing))
            else:
                items.append(rumps.MenuItem(label, callback=self._pair_output))
        if volume is not None:
            items.append(None)
            items.append(rumps.SliderMenuItem(
                value=volume, callback=self._volume_changed))
            items.append(rumps.MenuItem("Volume: {:.0f}% (loopback mode)".format(volume),
                                        callback=None))
        self.audio_item.menu = items

    def _volume_changed(self, sender) -> None:
        value = sender.value if hasattr(sender, "value") else sender
        # Debounce: the slider fires continuously while dragging.
        timer = getattr(self, "_vol_timer", None)
        if timer is not None:
            timer.cancel()
        self._vol_timer = threading.Timer(0.25, self._set_volume_worker,
                                          args=(float(value),))
        self._vol_timer.daemon = True
        self._vol_timer.start()

    def _set_volume_worker(self, percent: float) -> None:
        try:
            from hud.routing_fix import set_output_volume
            result = set_output_volume(percent)
            if not result.ok:
                rumps.notification("zoom-recorder", "Volume", result.message)
        except Exception as exc:
            rumps.notification("zoom-recorder", "Volume", str(exc))

    def toggle(self, _sender) -> None:
        pid = _read_pidfile()
        if pid is not None:
            self._stop(pid)
        else:
            self._start()
        self._sync_ui()

    def start_live(self, _sender) -> None:
        if _read_pidfile() is not None:
            return
        self._start(live=True)
        self._sync_ui()

    def open_hud(self, _sender) -> None:
        if not HUD_URLFILE.is_file():
            rumps.notification("zoom-recorder", "No HUD running",
                               "Start a recording with the Live HUD first.")
            return
        url = HUD_URLFILE.read_text(encoding="utf-8").strip()
        if url:
            subprocess.Popen(["open", url])

    def fix_routing(self, _sender) -> None:
        # Runs the routing fixer as a subprocess (never the app process);
        # it rebuilds the Multi-Output Device from the stored preference.
        threading.Thread(target=self._run_routing_fix, daemon=True).start()

    def _pair_output(self, sender) -> None:
        name = sender.title.lstrip("✓").strip()
        if not name:
            return
        threading.Thread(target=self._run_routing_fix, args=(name,), daemon=True).start()

    def restore_routing(self, _sender) -> None:
        threading.Thread(target=self._run_routing_restore, daemon=True).start()

    def _run_routing_restore(self) -> None:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "hud.routing_fix", "--restore-routing", "--yes"],
                cwd=str(SCRIPT_DIR), capture_output=True, text=True, timeout=90)
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:
            out = str(exc)
        lines = [ln for ln in out.splitlines()
                 if ln.strip() and not ln.startswith("RESULT: ")]
        detail = lines[0] if lines else "Restore finished."
        if "RESULT: MANUAL" in out:
            detail = "Restore failed; run ./zoom_record.py --restore-routing in Terminal."
        rumps.notification("zoom-recorder", "Audio routing", detail)

    def _run_routing_fix(self, output: "str | None" = None) -> None:
        args = [sys.executable, "-m", "hud.routing_fix", "--yes"]
        if output:
            args += ["--output", output]
        try:
            proc = subprocess.run(args, cwd=str(SCRIPT_DIR),
                                  capture_output=True, text=True, timeout=90)
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:
            out = str(exc)
        lines = [ln for ln in out.splitlines()
                 if ln.strip() and not ln.startswith("RESULT: ")]
        detail = "Fix unavailable; run ./zoom_record.py --fix-routing in Terminal."
        if "RESULT: OK" in out:
            detail = lines[0] if lines else detail
            if _read_pidfile() is not None:
                detail += " (recording unaffected)"
        elif "RESULT: MANUAL" in out:
            detail = ("Could not set it up automatically. Audio MIDI Setup opened; "
                      "follow the click-by-click steps (README: audio routing).")
            subprocess.Popen(["open", "-a", "Audio MIDI Setup"])
        rumps.notification("zoom-recorder", "Audio routing", detail)

    def open_settings(self, _sender) -> None:
        if _read_pidfile(SETTINGS_PIDFILE) is None:
            try:
                subprocess.Popen([sys.executable, str(SETTINGS)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as exc:
                rumps.notification("zoom-recorder", "Settings failed to start", str(exc))
                return
        threading.Thread(target=self._open_settings_when_ready, daemon=True).start()

    def _open_settings_when_ready(self) -> None:
        for _ in range(50):
            if SETTINGS_URLFILE.is_file():
                url = SETTINGS_URLFILE.read_text(encoding="utf-8").strip()
                if url:
                    subprocess.Popen(["open", url])
                    return
            time.sleep(0.1)
        rumps.notification("zoom-recorder", "Settings",
                           "Settings window did not start; run ./settings.py")

    def _start(self, live: bool = False) -> None:
        args = [sys.executable, str(RECORDER)]
        extra = os.environ.get("ZOOM_RECORDER_ARGS")
        if extra:
            args += extra.split()
        if live and "--live" not in args:
            args.append("--live")
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        PIDFILE.write_text(str(proc.pid))
        rumps.notification("zoom-recorder", "Recording started",
                           "Live HUD enabled" if live else "")

    def _stop(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        PIDFILE.unlink(missing_ok=True)
        rumps.notification("zoom-recorder", "Recording stopped", "Merging and verifying...")

    @rumps.timer(5)
    def _poll(self, _sender) -> None:
        # Catches the recorder exiting on its own (crash, no usable mic, etc.)
        # so the icon doesn't lie about whether we're actually recording.
        self._sync_ui()


if __name__ == "__main__":
    app = RecorderApp()
    print("zoom-recorder menubar started (pid {}); menu: {}".format(
        os.getpid(),
        " | ".join([app.toggle_item.title, app.live_item.title,
                    app.open_hud_item.title, app.settings_item.title])), flush=True)
    app.run()
