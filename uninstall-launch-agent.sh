#!/bin/bash
# Removes the menubar.py LaunchAgent installed by install-launch-agent.sh.
set -euo pipefail

LABEL="com.zoomrecorder.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "Removed $LABEL (plist and running instance, if any)."
