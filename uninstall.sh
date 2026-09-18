#!/bin/bash
# Removes everything zoom-recorder installed on this machine.
#
# Usage:
#   ./uninstall.sh                    # remove the login agent and state files
#   ./uninstall.sh --restore-routing  # also put your normal audio routing back
#   ./uninstall.sh --dry-run
#
# Your recordings under ~/ZoomRecordings are never touched.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.zoomrecorder.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
PYTHON3="$(command -v python3 || true)"
RESTORE=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --restore-routing) RESTORE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

echo "Removing zoom-recorder's login agent (if any)..."
run launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
run rm -f "$PLIST"

echo "Removing runtime state files..."
for f in "$HOME/.zoom_recorder.pid" "$HOME/.zoom_recorder_hud.url" \
         "$HOME/.zoom_recorder_settings.pid" "$HOME/.zoom_recorder_settings.url" \
         "$HOME/.zoom_recorder_routing.json"; do
  run rm -f "$f"
done

if [[ "$RESTORE" == "1" && -n "$PYTHON3" ]]; then
  echo "Restoring normal audio routing..."
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] $PYTHON3 -m hud.routing_fix --restore-routing"
  else
    (cd "$SCRIPT_DIR" && "$PYTHON3" -m hud.routing_fix --restore-routing) || true
  fi
fi

echo "Done. Recordings under ~/ZoomRecordings were left untouched."
echo "The BlackHole driver (if you installed it) is a separate brew package:"
echo "  brew uninstall blackhole-2ch"
