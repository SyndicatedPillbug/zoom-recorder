#!/usr/bin/env python3
"""Menu-bar app for zoom-recorder.

No background daemon watches for calls or auto-arms itself -- per the
2026-09-16 decision, a persistent process with silent mic access and file
writes is exactly what an EDR (Falcon) is tuned to flag, and it's a consent
risk if it ever records something it shouldn't. This app only exists, visibly,
in the menu bar; the recorder subprocess only exists while you're actually
recording, and the icon always shows which state you're in (🎙 idle,
🔴 recording, 🧠 recording with the transcript window up).

The menu is written for non-technical users: Volume (with the live level),
Start/Stop recording (with a timer), Open transcript window, My recordings,
Check my audio setup, Settings, Help, and Play sound through.

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
CONTROL_PIDFILE = Path.home() / ".zoom_recorder_control.pid"
CONTROL_URLFILE = Path.home() / ".zoom_recorder_control.url"

IDLE_TITLE = "🎙"


def live_transcript_available() -> bool:
    """True when a live transcript backend is configured (local or a key)."""
    try:
        from hud.config import load_config
        cfg = load_config()
        if cfg.provider_for_stt() is None:
            return True  # local backend
        return bool(cfg.api_key_for(cfg.stt_backend))
    except Exception:  # noqa: BLE001
        return False


def audio_menu_specs(outputs, paired):
    """Pure spec for the "Play sound through" dropdown: [(label, device_name)].

    label None means a separator; device_name None means "Rebuild Routing".
    """
    specs = [(("✓ " if name == paired else "") + name, name) for name in outputs]
    specs.append((None, None))
    specs.append(("Rebuild Routing", None))
    return specs


def populate_submenu(parent_item, entries) -> None:
    """Attach ``entries`` (rumps.MenuItem / rumps.SliderMenuItem / None for a
    separator) as the real submenu of ``parent_item``.

    rumps 0.4.0's ``MenuItem.menu`` assignment is a plain Python attribute --
    it never calls ``setSubmenu_``, so the parent renders with no children and
    macOS greys it out. It also cannot carry a SliderMenuItem. Attaching the
    NSMenu ourselves sidesteps both; callbacks still dispatch because
    MenuItem/SliderMenuItem registered them with NSApp on construction.
    """
    from AppKit import NSMenu, NSMenuItem

    menu = parent_item._menuitem.submenu()
    if menu is None:
        menu = NSMenu.alloc().init()
        parent_item._menuitem.setSubmenu_(menu)
    menu.removeAllItems()
    for entry in entries:
        if entry is None:
            menu.addItem_(NSMenuItem.separatorItem())
        else:
            menu.addItem_(entry._menuitem)
    parent_item._menuitem.setEnabled_(True)


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
        super().__init__(IDLE_TITLE, quit_button=None)
        # Volume is deliberately the first top-level item so the current
        # level is visible at all times (its title carries the percentage).
        self.volume_item = rumps.MenuItem("Volume")
        self.toggle_item = rumps.MenuItem("Start recording", callback=self.toggle)
        self.live_item = rumps.MenuItem("Start with live transcript", callback=self.start_live)
        self.open_hud_item = rumps.MenuItem("Open transcript window", callback=self.open_hud)
        self.recordings_item = rumps.MenuItem("My recordings…",
                                              callback=lambda _s: self.open_control("recordings"))
        self.setup_item = rumps.MenuItem("Check my audio setup…",
                                         callback=lambda _s: self.open_control("setup"))
        self.settings_item = rumps.MenuItem("Settings…",
                                            callback=lambda _s: self.open_control("settings"))
        self.help_item = rumps.MenuItem("Help",
                                        callback=lambda _s: self.open_control("help"))
        self.audio_item = rumps.MenuItem("Play sound through")
        self.quit_item = rumps.MenuItem("Quit zoom-recorder", callback=self.quit_app)
        self.menu = [self.volume_item, self.toggle_item, self.live_item,
                     self.open_hud_item, None,
                     self.recordings_item, self.setup_item, self.settings_item,
                     self.help_item, None, self.audio_item, None, self.quit_item]
        self._audio_sig = None
        self._volume_value = None
        self._volume_muted = None
        self._vol_drag_until = 0.0
        self._sync_ui()
        # Recover from a previous SIGKILL/crash that left the recording audio
        # setup selected: hand the real device back when nothing is recording.
        try:
            from hud import routing_fix
            if _read_pidfile() is None and routing_fix.is_loopback_active():
                routing_fix.deactivate_loopback()
        except Exception:  # noqa: BLE001
            pass
        # First run: open the Setup wizard once so a new user is guided.
        try:
            from hud.config import load_config
            if not load_config().onboarded:
                self.open_control("setup")
        except Exception:  # noqa: BLE001
            pass

    def _hud_active(self) -> bool:
        # The HUD writes its URL file on start and removes it on stop, so its
        # presence alongside a live recorder means the HUD is actually up.
        return HUD_URLFILE.is_file() and _read_pidfile() is not None

    def _sync_ui(self) -> None:
        pid = _read_pidfile()
        recording = pid is not None
        elapsed = 0.0
        if recording:
            try:
                elapsed = max(0.0, time.time() - PIDFILE.stat().st_mtime)
            except OSError:
                elapsed = 0.0
        state = describe(recording, self._hud_active(), elapsed_s=elapsed,
                         live_available=live_transcript_available())
        self.title = state["icon"]
        self.toggle_item.title = state["toggle_title"]
        self.live_item.title = state["live_title"]
        self.live_item.set_callback(self.start_live if state["live_enabled"] else None)
        self.open_hud_item.set_callback(self.open_hud if state["open_enabled"] else None)
        self._sync_volume_if_changed()
        self._sync_audio_menu()

    def open_control(self, tab: str) -> None:
        """Open the Control Center (Setup/Recordings/Settings/Help).

        Reuses a running instance when there is one; otherwise starts it in
        the background (it shuts itself down when idle).
        """
        pid = _read_pidfile(CONTROL_PIDFILE)
        if pid is not None and CONTROL_URLFILE.is_file():
            url = CONTROL_URLFILE.read_text(encoding="utf-8").strip()
            if url:
                from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
                parts = urlsplit(url)
                token = (parse_qs(parts.query).get("token") or [""])[0]
                target = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                     urlencode({"token": token, "tab": tab}), ""))
                subprocess.Popen(["open", target])
                return
        try:
            subprocess.Popen(
                [sys.executable, "-m", "hud.control", "--tab", tab],
                cwd=str(SCRIPT_DIR),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            rumps.notification("zoom-recorder", "Could not open the window", str(exc))

    def _read_volume(self):
        try:
            from hud.routing_fix import get_output_volume, get_output_mute
            return get_output_volume(), bool(get_output_mute())
        except Exception:
            return None, False

    def _sync_volume_menu(self) -> None:
        """Build/refresh the top-level Volume menu (slider, +/- , mute,
        presets). Runs on construction, after every action, and on the poll
        so the label/title always match the real device volume."""
        from hud.routing_fix import format_volume_label, format_volume_title
        volume, muted = self._read_volume()
        if volume is None:
            self.volume_item.title = "Volume"
            populate_submenu(self.volume_item, [
                rumps.MenuItem("(no controllable output)", callback=None)])
            return
        self._volume_value, self._volume_muted = volume, muted
        self.volume_item.title = format_volume_title(volume, muted)
        self._volume_slider = rumps.SliderMenuItem(value=volume,
                                                   callback=self._volume_changed)
        self._volume_label = rumps.MenuItem(format_volume_label(volume, muted),
                                            callback=None)
        entries = [
            self._volume_slider,
            self._volume_label,
            None,
            rumps.MenuItem("Volume +5%", callback=lambda _s: self._nudge_volume(5)),
            rumps.MenuItem("Volume \u22125%", callback=lambda _s: self._nudge_volume(-5)),
            rumps.MenuItem("Unmute" if muted else "Mute", callback=self._toggle_mute),
            None,
        ]
        presets = rumps.MenuItem("Presets")
        populate_submenu(presets, [
            rumps.MenuItem("{}%".format(p),
                           callback=lambda _s, pct=p: self._set_volume(pct))
            for p in (25, 50, 75, 100)])
        entries.append(presets)
        populate_submenu(self.volume_item, entries)

    def _sync_volume_if_changed(self) -> None:
        # Don't fight the user mid-drag, and don't rebuild while idle unless
        # the underlying value actually moved (e.g. changed from the CLI).
        if time.time() < self._vol_drag_until:
            return
        volume, muted = self._read_volume()
        if (volume, muted) != (self._volume_value, self._volume_muted):
            self._sync_volume_menu()

    def _set_volume(self, percent: float) -> None:
        try:
            from hud.routing_fix import set_output_volume
            result = set_output_volume(percent)
            if not result.ok:
                rumps.notification("zoom-recorder", "Volume", result.message)
        except Exception as exc:  # noqa: BLE001
            rumps.notification("zoom-recorder", "Volume", str(exc))
        self._sync_volume_menu()

    def _nudge_volume(self, delta: float) -> None:
        try:
            from hud.routing_fix import change_output_volume
            result = change_output_volume(delta)
            if not result.ok:
                rumps.notification("zoom-recorder", "Volume", result.message)
        except Exception as exc:  # noqa: BLE001
            rumps.notification("zoom-recorder", "Volume", str(exc))
        self._sync_volume_menu()

    def _toggle_mute(self, _sender) -> None:
        try:
            from hud.routing_fix import set_output_mute
            result = set_output_mute(not bool(self._volume_muted))
            if not result.ok:
                rumps.notification("zoom-recorder", "Volume", result.message)
        except Exception as exc:  # noqa: BLE001
            rumps.notification("zoom-recorder", "Volume", str(exc))
        self._sync_volume_menu()

    def _sync_audio_menu(self) -> None:
        # Rebuild the "Audio Out" submenu only when the device set actually
        # changed (this runs on the 5s poll; CoreAudio reads are cheap but
        # rebuilding menu items every tick would reset their callbacks).
        try:
            from hud.routing_fix import list_real_outputs, load_state
            outputs = list_real_outputs()
            paired = load_state().get("physical_name")
        except Exception:
            outputs, paired = [], None
        sig = (tuple(outputs), paired)
        if sig == self._audio_sig:
            return
        self._audio_sig = sig
        # Recordings use loopback capture, so the pairing controls are always
        # relevant here; Restore Normal Routing undoes the routing entirely.
        entries = []
        for label, name in audio_menu_specs(outputs, paired):
            if label is None:
                entries.append(None)
            elif name is None:
                entries.append(rumps.MenuItem(label, callback=self.fix_routing))
            else:
                entries.append(rumps.MenuItem(label, callback=self._pair_output))
        entries.append(None)
        entries.append(rumps.MenuItem("Reset audio…",
                                      callback=self.restore_routing))
        populate_submenu(self.audio_item, entries)

    def _volume_changed(self, sender) -> None:
        value = sender.value if hasattr(sender, "value") else sender
        # Guard the poll sync: a rebuild mid-drag would reset the NSSlider.
        self._vol_drag_until = time.time() + 1.0
        timer = getattr(self, "_vol_timer", None)
        if timer is not None:
            timer.cancel()
        self._vol_timer = threading.Timer(0.25, self._set_volume_worker,
                                          args=(float(value),))
        self._vol_timer.daemon = True
        self._vol_timer.start()

    def _set_volume_worker(self, percent: float) -> None:
        try:
            from hud.routing_fix import (format_volume_label,
                                         format_volume_title,
                                         set_output_volume)
            result = set_output_volume(percent)
            if not result.ok:
                rumps.notification("zoom-recorder", "Volume", result.message)
                return
            # Update the visible state in place (no menu rebuild while the
            # user may still be dragging).
            self._volume_value = percent
            self.volume_item.title = format_volume_title(percent, self._volume_muted)
            label = getattr(self, "_volume_label", None)
            if label is not None:
                label.title = format_volume_label(percent, self._volume_muted)
        except Exception as exc:
            rumps.notification("zoom-recorder", "Volume", str(exc))

    def toggle(self, _sender) -> None:
        pid = _read_pidfile()
        if pid is not None:
            self._stop(pid)
        else:
            self._start()
        self._sync_ui()

    def quit_app(self, _sender) -> None:
        """Quit, but never orphan a recording: stop it and wait for the save."""
        pid = _read_pidfile()
        if pid is not None:
            rumps.notification("zoom-recorder", "Finishing…",
                               "Saving your recording before quitting")
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                pid = None
        if pid is not None:
            deadline = time.time() + 120
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.5)
        PIDFILE.unlink(missing_ok=True)
        rumps.quit_application()

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

    def _start(self, live: bool = False) -> None:
        # Refuse a second recorder (another menu-bar instance or the CLI).
        try:
            from hud.config import load_config
            from zoom_record import recorder_running
            basedir = Path(load_config().recorder.basedir).expanduser()
            if recorder_running(basedir):
                rumps.notification("zoom-recorder", "Already recording",
                                   "Another recording is in progress.")
                return
        except Exception:  # noqa: BLE001
            pass
        args = [sys.executable, str(RECORDER)]
        extra = os.environ.get("ZOOM_RECORDER_ARGS")
        if extra:
            args += extra.split()
        if live and "--live" not in args:
            args.append("--live")
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        PIDFILE.write_text(str(proc.pid))
        rumps.notification("zoom-recorder", "Recording started",
                           "Open the transcript window from the 🎙 menu"
                           if live else "Click 🎙 again to stop")

    def _stop(self, pid: int) -> None:
        # Send the clean stop signal but keep the pidfile until the process
        # actually exits: it still has to merge, verify and archive, and a
        # second recording must not start in the meantime.
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        rumps.notification("zoom-recorder", "Finishing…",
                           "Saving your recording and transcript")

    @rumps.timer(1)
    def _poll(self, _sender) -> None:
        # Catches the recorder exiting on its own (crash, no usable mic, etc.)
        # so the icon doesn't lie about whether we're actually recording. Also
        # keeps the elapsed timer in the toggle title ticking.
        self._sync_ui()


if __name__ == "__main__":
    app = RecorderApp()
    print("zoom-recorder menubar started (pid {}); menu: {}".format(
        os.getpid(),
        " | ".join([app.volume_item.title, app.toggle_item.title,
                    app.live_item.title, app.open_hud_item.title,
                    app.recordings_item.title, app.setup_item.title,
                    app.settings_item.title, app.help_item.title,
                    app.audio_item.title])), flush=True)
    app.run()
