#!/usr/bin/env python3
"""Pure menu-state logic for the menu-bar app.

Kept free of any ``rumps``/AppKit import so it can be unit-tested headlessly.
``menubar.py`` renders whatever :func:`describe` returns.
"""

from __future__ import annotations

from typing import Any, Dict

ICON_IDLE = "🎙"
ICON_RECORDING = "🔴 REC"
ICON_HUD = "🧠 HUD"


def describe(recording: bool, hud_active: bool) -> Dict[str, Any]:
    """Return the icon and item labels/enabled-state for the current session.

    ``hud_active`` is only ever true while recording, since the HUD lives
    inside the recorder process.
    """
    if hud_active:
        icon = ICON_HUD
    elif recording:
        icon = ICON_RECORDING
    else:
        icon = ICON_IDLE

    toggle_title = "Stop Recording" if recording else "Start Recording"

    if hud_active:
        live_title = "Live HUD active ✓"
        live_enabled = False
    else:
        live_title = "Start with Live HUD"
        # The HUD can only be attached at recording start, so this is disabled
        # while a plain recording is already running.
        live_enabled = not recording

    return {
        "icon": icon,
        "toggle_title": toggle_title,
        "live_title": live_title,
        "live_enabled": live_enabled,
        "open_enabled": hud_active,
    }
