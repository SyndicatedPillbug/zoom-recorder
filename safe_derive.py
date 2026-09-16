#!/usr/bin/env python3
"""Guard wrapper for running third-party tools (enhancers, denoisers, etc.)
against a recording without risking the original.

On 2026-09-16 a third-party audio "enhancer" was run with its output path
set to the same file as the only copy of a recording, zeroing it out. This
wrapper refuses to run a command if its declared --out path resolves inside
a recording session folder (identified structurally by the capture.log that
every session writes -- not by folder name, since that's changed once
already) outside that folder's derived/ subdirectory, the one place
downstream tools are supposed to write to.

Usage:
    ./safe_derive.py --out ~/ZoomRecordings/2026-09-16/15-26-03_a1b2c3f4/derived/enhanced.wav -- \\
        some-enhancer --in ~/ZoomRecordings/2026-09-16/15-26-03_a1b2c3f4/recording_mic.wav \\
                      --out ~/ZoomRecordings/2026-09-16/15-26-03_a1b2c3f4/derived/enhanced.wav
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


def find_session_root(path: Path) -> Optional[Path]:
    """Walk up from `path` looking for the folder a recording actually lives
    in -- marked by the capture.log every session writes, regardless of how
    the folder itself is named."""
    for parent in path.parents:
        if (parent / "capture.log").is_file():
            return parent
    return None


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="the output path the wrapped command will write to")
    parser.add_argument("cmd", nargs=argparse.REMAINDER,
                        help="the command to run, e.g. -- some-enhancer --out ...")
    args = parser.parse_args(argv)

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("safe_derive: no command given after --", file=sys.stderr)
        return 2

    out_path = Path(args.out).expanduser().resolve()
    session_root = find_session_root(out_path)
    if session_root is not None:
        try:
            out_path.relative_to(session_root / "derived")
        except ValueError:
            print(
                "safe_derive: REFUSED. Output path {} resolves inside the recording "
                "session {} but outside its derived/ subfolder.\n"
                "This is exactly how the 2026-09-16 recording was destroyed -- point "
                "the output at {}/derived/ instead.".format(out_path, session_root, session_root),
                file=sys.stderr,
            )
            return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
