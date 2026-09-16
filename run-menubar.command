#!/bin/bash
# Double-click this in Finder (or run it) any time the menu-bar toggle isn't
# up for some reason -- crashed, killed, machine just booted before the
# LaunchAgent caught up, etc. Safe to run even if it's already running.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.zoomrecorder.menubar"
UID_NUM="$(id -u)"

if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  echo "Restarting the zoom-recorder menu-bar app via launchd..."
  launchctl kickstart -k "gui/$UID_NUM/$LABEL"
  echo "Done. Look for 🎙 in the menu bar."
else
  echo "No LaunchAgent installed (run ./install-launch-agent.sh once to fix that)."
  if pgrep -f "$SCRIPT_DIR/menubar.py" >/dev/null 2>&1; then
    echo "menubar.py is already running directly. Nothing to do."
  else
    echo "Starting it directly (won't survive logout -- install the LaunchAgent for that)..."
    nohup python3 "$SCRIPT_DIR/menubar.py" >/tmp/zoom-recorder-menubar.log 2>&1 &
    disown
    sleep 1
    if pgrep -f "$SCRIPT_DIR/menubar.py" >/dev/null 2>&1; then
      echo "Started. Look for 🎙 in the menu bar."
    else
      echo "Failed to start -- check /tmp/zoom-recorder-menubar.log"
    fi
  fi
fi

read -n 1 -s -r -p "Press any key to close this window..."
echo
