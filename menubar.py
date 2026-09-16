#!/usr/bin/env python3
"""One-keystroke-equivalent activation: a menu-bar toggle for zoom_record.py.

No background daemon watches for calls or auto-arms itself -- per the
2026-09-16 decision, a persistent process with silent mic access and file
writes is exactly what an EDR (Falcon) is tuned to flag, and it's a consent
risk if it ever records something it shouldn't. This app only exists, visibly,
in the menu bar; the recorder subprocess only exists while you're actually
recording, and the icon always shows which state you're in.

Usage:
    pip3 install --user rumps
    ./menubar.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import rumps

SCRIPT_DIR = Path(__file__).resolve().parent
RECORDER = SCRIPT_DIR / "zoom_record.py"
PIDFILE = Path.home() / ".zoom_recorder.pid"

IDLE_TITLE = "🎙"
RECORDING_TITLE = "🔴 REC"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pidfile() -> "int | None":
    if not PIDFILE.is_file():
        return None
    try:
        pid = int(PIDFILE.read_text().strip())
    except ValueError:
        PIDFILE.unlink(missing_ok=True)
        return None
    if not _pid_alive(pid):
        PIDFILE.unlink(missing_ok=True)
        return None
    return pid


class RecorderApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(IDLE_TITLE, quit_button="Quit")
        self.toggle_item = rumps.MenuItem("Start Recording", callback=self.toggle)
        self.menu = [self.toggle_item]
        self._sync_ui()

    def _sync_ui(self) -> None:
        recording = _read_pidfile() is not None
        self.title = RECORDING_TITLE if recording else IDLE_TITLE
        self.toggle_item.title = "Stop Recording" if recording else "Start Recording"

    def toggle(self, _sender) -> None:
        pid = _read_pidfile()
        if pid is not None:
            self._stop(pid)
        else:
            self._start()
        self._sync_ui()

    def _start(self) -> None:
        args = [sys.executable, str(RECORDER)]
        extra = os.environ.get("ZOOM_RECORDER_ARGS")
        if extra:
            args += extra.split()
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        PIDFILE.write_text(str(proc.pid))
        rumps.notification("zoom-recorder", "Recording started", "")

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
    RecorderApp().run()
