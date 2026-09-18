#!/bin/bash
# zoom-recorder installer.
#
# Checks (and, with --install-deps, installs) everything the tool needs, then
# optionally enables login autostart and runs the environment doctor.
#
# Usage:
#   ./install.sh                  # check deps, report, run doctor
#   ./install.sh --install-deps   # also brew/pip install what is missing
#   ./install.sh --autostart      # also enable login autostart (opt-in)
#   ./install.sh --dry-run        # print what would happen, change nothing
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON3="$(command -v python3 || true)"
INSTALL_DEPS=0
AUTOSTART=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --install-deps) INSTALL_DEPS=1 ;;
    --autostart) AUTOSTART=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    say "  [dry-run] $*"
  else
    "$@"
  fi
}

say "zoom-recorder installer"
say "======================="
say "This tool records your microphone and, through the standard third-party"
say "BlackHole loopback driver, the other party's audio. It does not request"
say "Screen Recording, ships no driver, and sends nothing off the machine"
say "unless you explicitly enable --live with your own API key."
say ""

say "macOS: $(sw_vers -productVersion 2>/dev/null || echo unknown)"
if [[ -z "$PYTHON3" ]]; then
  say "ERROR: python3 not found. Install the Xcode Command Line Tools:"
  say "  xcode-select --install"
  exit 1
fi
say "python3: $PYTHON3 ($("$PYTHON3" -c 'import sys; print(sys.version.split()[0])'))"

missing=0

# --- ffmpeg / ffprobe -----------------------------------------------------
for tool in ffmpeg ffprobe; do
  if command -v "$tool" >/dev/null 2>&1; then
    say "$tool: $(command -v "$tool")"
  else
    missing=1
    say "$tool: MISSING"
    if [[ "$INSTALL_DEPS" == "1" ]]; then
      run brew install ffmpeg
    else
      say "  install: brew install ffmpeg"
    fi
  fi
done

# --- rumps (menu bar UI) --------------------------------------------------
if "$PYTHON3" -c "import rumps" >/dev/null 2>&1; then
  say "rumps: installed"
else
  missing=1
  say "rumps: MISSING (only needed for the menu-bar app)"
  if [[ "$INSTALL_DEPS" == "1" ]]; then
    run "$PYTHON3" -m pip install --user rumps
  else
    say "  install: pip3 install --user rumps"
  fi
fi

# --- BlackHole (system-audio loopback) ------------------------------------
if "$PYTHON3" - "$SCRIPT_DIR" <<'PY' >/dev/null 2>&1
import sys
sys.path.insert(0, sys.argv[1])
from hud.devices import read_system_profiler
print("yes" if any(d.name.startswith("BlackHole") for d in read_system_profiler().devices) else "")
PY
then
  say "BlackHole: installed"
else
  missing=1
  say "BlackHole: MISSING (needed to capture the other party's audio)"
  if [[ "$INSTALL_DEPS" == "1" ]]; then
    run brew install blackhole-2ch
  else
    say "  install: brew install blackhole-2ch   (asks for your admin password)"
  fi
fi

say ""
if [[ "$missing" == "1" && "$INSTALL_DEPS" == "0" ]]; then
  say "Some dependencies are missing. Re-run with --install-deps to install them,"
  say "or run the commands above yourself."
  say ""
fi

# --- optional login autostart ---------------------------------------------
if [[ "$AUTOSTART" == "1" ]]; then
  say "Enabling login autostart (opt-in)..."
  if [[ "$DRY_RUN" == "1" ]]; then
    say "  [dry-run] ./install-launch-agent.sh --dry-run"
  else
    "$SCRIPT_DIR/install-launch-agent.sh"
  fi
else
  say "Login autostart: not enabled (optional). Start the menu bar with"
  say "  ./run-menubar.command"
  say "or enable autostart with: ./install.sh --autostart"
fi

# --- doctor ---------------------------------------------------------------
say ""
say "Checking the environment..."
if [[ "$DRY_RUN" == "1" ]]; then
  say "  [dry-run] $PYTHON3 $SCRIPT_DIR/zoom_record.py --doctor"
else
  "$PYTHON3" "$SCRIPT_DIR/zoom_record.py" --doctor || true
fi

say ""
say "Done. Open the menu bar (./run-menubar.command), then 'Start with Live HUD'"
say "or 'Start Recording'. See INSTALL.md for the one-time microphone grant and"
say "SECURITY.md for exactly what this tool accesses, writes and (optionally)"
say "sends over the network."
