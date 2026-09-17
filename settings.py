#!/usr/bin/env python3
"""Launcher for the HUD settings GUI.

Thin wrapper so the menu bar (and you) can start the settings page with
``./settings.py`` -- it opens a browser to a localhost form that edits
``~/.config/zoom-recorder/config.json``. See ``hud/settings.py``.

Usage:
    ./settings.py [--port N] [--no-browser] [--timeout SECONDS]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hud.settings import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
