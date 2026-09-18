#!/bin/bash
# Login autostart for the menu-bar app (OPTIONAL).
#
# The default way to use the menu bar is ./run-menubar.command, which starts
# nothing at login and leaves no persistence. This script is the explicit
# opt-in: it installs a per-user LaunchAgent that starts the menu bar when you
# log in. No recording daemon is installed -- the recorder subprocess still
# only exists while you are actually recording.
#
# Modes:
#   (no flags)            full install: (re)load the job and start it now
#   --enable-autostart    make it start at login WITHOUT touching a running app
#   --disable-autostart   stop it starting at login, leave the running app alone
#   --disable             alias of --disable-autostart
#   --dry-run             print what would happen, change nothing
#
# The GUI (Control Center) uses only the --*-autostart modes: a full install
# does `launchctl bootout`, which would kill the menu bar and anything it
# spawned -- including the Control Center asking for the change.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.zoomrecorder.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON3="$(command -v python3 || true)"
LOG_DIR="$HOME/Library/Logs"
UID_NUM="$(id -u)"
DRY_RUN=0
MODE="install"          # install | enable | disable

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --enable-autostart) MODE="enable" ;;
    --disable-autostart|--disable) MODE="disable" ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

job_loaded() {
  launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1
}

plist_content() {
  cat <<PLIST_EOF
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
}

# ---------------------------------------------------------------- disable --
if [[ "$MODE" == "disable" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] launchctl disable gui/$UID_NUM/$LABEL; rm -f $PLIST"
    exit 0
  fi
  launchctl disable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Login autostart disabled."
  echo "  $PLIST removed; it will not start at your next login."
  if job_loaded; then
    echo "  (It is still running now -- quitting it is up to you.)"
  fi
  exit 0
fi

# --------------------------------------------------------------- preflight --
if [[ ! -x "$PYTHON3" ]]; then
  echo "ERROR: python3 not found on PATH." >&2
  exit 1
fi
if ! "$PYTHON3" -c "import rumps" 2>/dev/null; then
  echo "ERROR: rumps is not installed for $PYTHON3. Run: pip3 install --user rumps" >&2
  exit 1
fi

CONTENT="$(plist_content)"

# --------------------------------------------------------------- enable ----
# Write the plist and enable it. If the job is already loaded, leave it
# running exactly as it is (no bootout, no kickstart): autostart applies from
# the next login. Only bootstrap when nothing is loaded, which also starts it
# now (safe: there is no running instance to disturb).
if [[ "$MODE" == "enable" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] write $PLIST; launchctl enable gui/$UID_NUM/$LABEL"
    exit 0
  fi
  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
  printf '%s\n' "$CONTENT" > "$PLIST"
  launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  if job_loaded; then
    echo "Login autostart enabled (it is already running; no restart needed)."
  else
    launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null || true
    echo "Login autostart enabled and the menu bar started."
  fi
  exit 0
fi

# -------------------------------------------------------------- install ----
if [[ "$DRY_RUN" == "1" ]]; then
  echo "Would write $PLIST:"
  echo "$CONTENT"
  echo "(dry run: nothing installed)"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
printf '%s\n' "$CONTENT" > "$PLIST"

# Unload any previous copy first so re-running picks up changes (path moved,
# python upgraded, etc.) instead of silently keeping stale config. NOTE: this
# kills the running menu bar and anything it spawned -- never call it from the
# GUI; use --enable-autostart there.
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
