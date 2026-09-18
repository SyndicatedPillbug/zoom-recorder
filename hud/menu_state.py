#!/usr/bin/env python3
"""Pure menu-state logic for the menu-bar app.

Kept free of any ``rumps``/AppKit import so it can be unit-tested headlessly.
``menubar.py`` renders whatever :func:`describe` returns.
"""

from __future__ import annotations

from typing import Any, Dict

ICON_IDLE = "🎙"
ICON_RECORDING = "🔴"
ICON_HUD = "🧠"


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "{}:{:02d}:{:02d}".format(hours, minutes, secs)
    return "{:d}:{:02d}".format(minutes, secs)


def describe(recording: bool, hud_active: bool, elapsed_s: float = 0.0,
             live_available: bool = True) -> Dict[str, Any]:
    """Return the icon and item labels/enabled-state for the current session.

    ``hud_active`` is only ever true while recording, since the transcript
    window lives inside the recorder process.
    """
    if hud_active:
        icon = ICON_HUD
    elif recording:
        icon = ICON_RECORDING
    else:
        icon = ICON_IDLE

    if recording:
        toggle_title = "Stop recording ({})".format(format_elapsed(elapsed_s))
    else:
        toggle_title = "Start recording"

    if hud_active:
        live_title = "Live transcript active ✓"
        live_enabled = False
    elif not live_available:
        live_title = "Start with live transcript (set up first)"
        live_enabled = False
    else:
        live_title = "Start with live transcript"
        # The transcript window can only be attached at recording start.
        live_enabled = not recording

    return {
        "icon": icon,
        "toggle_title": toggle_title,
        "live_title": live_title,
        "live_enabled": live_enabled,
        "open_enabled": hud_active,
    }
