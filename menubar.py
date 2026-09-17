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
        self.fix_routing_item = rumps.MenuItem("Fix Audio Routing…", callback=self.fix_routing)
        self.menu = [self.toggle_item, self.live_item, None,
                     self.open_hud_item, self.settings_item, self.fix_routing_item]
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
        # it creates the Multi-Output Device and switches the default output.
        self.fix_routing_item.title = "Fixing Audio Routing…"
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "hud.routing_fix"],
                cwd=str(SCRIPT_DIR), capture_output=True, text=True, timeout=90)
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:
            out = str(exc)
        self.fix_routing_item.title = "Fix Audio Routing…"
        lines = [ln for ln in out.splitlines()
                 if ln.strip() and not ln.startswith("RESULT: ")]
        detail = "Fix unavailable; run ./zoom_record.py --fix-routing in Terminal."
        if "RESULT: OK" in out:
            detail = lines[0] if lines else detail
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
