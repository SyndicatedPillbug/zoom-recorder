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
        super().__init__(IDLE_TITLE, quit_button="Quit")
        # Volume is deliberately the first top-level item so the current
        # level is visible at all times (its title carries the percentage).
        self.volume_item = rumps.MenuItem("Volume")
        self.toggle_item = rumps.MenuItem("Start Recording", callback=self.toggle)
        self.live_item = rumps.MenuItem("Start with Live HUD", callback=self.start_live)
        self.open_hud_item = rumps.MenuItem("Open Live HUD…", callback=self.open_hud)
        self.settings_item = rumps.MenuItem("Settings…", callback=self.open_settings)
        self.audio_item = rumps.MenuItem("Audio Out")
        self.menu = [self.volume_item, self.toggle_item, self.live_item, None,
                     self.open_hud_item, self.settings_item, self.audio_item]
        self._audio_sig = None
        self._volume_value = None
        self._volume_muted = None
        self._vol_drag_until = 0.0
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
        self._sync_volume_if_changed()
        self._sync_audio_menu()

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
        entries.append(rumps.MenuItem("Restore Normal Routing…",
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
