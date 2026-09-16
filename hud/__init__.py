"""Live transcription + AI answer HUD for zoom-recorder.

This package is intentionally opt-in: nothing here runs unless the recorder is
launched with ``--live``. Every subsystem is isolated from the capture path, so
a crash (or a missing API key) degrades the HUD without ever endangering the
recording or its verification.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
