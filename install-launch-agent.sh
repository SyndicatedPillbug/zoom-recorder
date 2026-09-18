#!/bin/bash
# Enables login autostart for the menu-bar app (OPTIONAL).
#
# The default way to use the menu bar is ./run-menubar.command, which starts
# nothing at login and leaves no persistence. This script is the explicit
# opt-in: it installs a per-user LaunchAgent that starts the menu bar when you
# log in. No recording daemon is installed -- the recorder subprocess still
# only exists while you are actually recording.
#
# Usage:
#   ./install-launch-agent.sh [--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.zoomrecorder.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON3="$(command -v python3)"
LOG_DIR="$HOME/Library/Logs"
UID_NUM="$(id -u)"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -x "$PYTHON3" ]]; then
  echo "ERROR: python3 not found on PATH." >&2
  exit 1
fi
if ! "$PYTHON3" -c "import rumps" 2>/dev/null; then
  echo "ERROR: rumps is not installed for $PYTHON3. Run: pip3 install --user rumps" >&2
  exit 1
fi

PLIST_CONTENT="$(cat <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON3</string>
        <string>$SCRIPT_DIR/menubar.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/zoom-recorder-menubar.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/zoom-recorder-menubar.log</string>
</dict>
</plist>
PLIST_EOF
)"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "Would write $PLIST:"
  echo "$PLIST_CONTENT"
  echo "(dry run: nothing installed)"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
printf '%s\n' "$PLIST_CONTENT" > "$PLIST"

# Unload any previous copy first so re-running picks up changes (path moved,
# python upgraded, etc.) instead of silently keeping stale config.
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true

launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo "Installed and started: $LABEL"
echo "  plist:  $PLIST"
echo "  log:    $LOG_DIR/zoom-recorder-menubar.log"
echo "It will now also start automatically at login."
echo "Look for 🎙 in the menu bar. If you don't see it, check the log above."
echo "Remove it any time with ./uninstall.sh"
