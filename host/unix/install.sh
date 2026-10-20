#!/usr/bin/env bash
# Install the now-playing agent so it starts at login (Linux: systemd user service,
# macOS: LaunchAgent). No root needed.
#
#   ./install.sh               install + start
#   ./install.sh --uninstall   stop + remove
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NAME=thingdeck-agent

need() { command -v "$1" >/dev/null || { echo "missing: $1. $2"; exit 1; }; }

case "$(uname -s)" in
Linux)
    DIR="$HOME/.local/share/thingdeck"
    UNIT="$HOME/.config/systemd/user/$NAME.service"
    if [ "${1:-}" = "--uninstall" ]; then
        systemctl --user disable --now "$NAME" 2>/dev/null || true
        rm -f "$UNIT" "$DIR/nowplaying.py"
        systemctl --user daemon-reload
        echo "removed"; exit 0
    fi
    need python3 "Install Python 3."
    need adb "Fedora: sudo dnf install android-tools   Debian/Ubuntu: sudo apt install adb"
    need playerctl "Fedora: sudo dnf install playerctl   Debian/Ubuntu: sudo apt install playerctl"
    mkdir -p "$DIR" "$(dirname "$UNIT")"
    cp "$HERE/nowplaying.py" "$DIR/nowplaying.py"
    cat > "$UNIT" <<EOF
[Unit]
Description=thingdeck agent: send the Car Thing the time, what is playing, stats, calendars and GitHub status over adb
# Start after the desktop has exported WAYLAND_DISPLAY, DISPLAY and XDG_CURRENT_DESKTOP:
# the focused app and xdg-open need them, and default.target is reached before that.
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=$(command -v python3) $DIR/nowplaying.py
Environment=ADB=$(command -v adb)
Restart=always
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$NAME"
    systemctl --user restart "$NAME"
    echo "installed and started. Log: journalctl --user -u $NAME -f"
    ;;
Darwin)
    DIR="$HOME/Library/Application Support/thingdeck"
    PLIST="$HOME/Library/LaunchAgents/com.thingdeck.agent.plist"
    if [ "${1:-}" = "--uninstall" ]; then
        launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
        rm -f "$PLIST" "$DIR/nowplaying.py"
        echo "removed"; exit 0
    fi
    need python3 "Install it with: xcode-select --install (or brew install python)"
    need adb "brew install android-platform-tools"
    command -v nowplaying-cli >/dev/null || echo "note: nowplaying-cli not found (brew install nowplaying-cli); falling back to Spotify/Music via AppleScript"
    mkdir -p "$DIR" "$(dirname "$PLIST")"
    cp "$HERE/nowplaying.py" "$DIR/nowplaying.py"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.thingdeck.agent</string>
  <key>ProgramArguments</key>
  <array><string>$(command -v python3)</string><string>$DIR/nowplaying.py</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>ADB</key><string>$(command -v adb)</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$DIR/nowplaying.log</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "installed and started. Log: $DIR/nowplaying.log"
    echo "macOS may ask to allow controlling Spotify/Music (Automation) the first time."
    echo "For Hue, Plex and TrueNAS, turn on Python in System Settings > Privacy & Security > Local Network."
    ;;
*)
    echo "this installer is for Linux and macOS; on Windows use host/windows/install.ps1"; exit 1
    ;;
esac
