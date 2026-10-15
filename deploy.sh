#!/usr/bin/env bash
# Push device/ to the Car Thing over ADB and restart the macro pad.
#
#   ./deploy.sh              copy files + restart the service
#   ./deploy.sh config       copy config.json only (reloaded live, no restart)
#   ./deploy.sh install      first install: copy files + enable start on boot + reboot
#   ./deploy.sh uninstall    remove the boot hook and reboot to the stock UI
#
# Works in Git Bash on Windows and on Linux. Set ADB=/path/to/adb if adb is not on PATH.
set -euo pipefail
export MSYS_NO_PATHCONV=1          # stop Git Bash rewriting /var/... into C:/Program Files/Git/var/...

ADB=${ADB:-adb}
# Git Bash: adb.exe needs a Windows path (pwd -W); elsewhere plain pwd.
SRC="$(cd "$(dirname "$0")/device" && { pwd -W 2>/dev/null || pwd; })"
DEST=/var/macropad
FILES="macropad.py gadget.sh start.sh macropad.conf config.json web/index.html web/shim.html tools/cdp.py"

push_files() {
    "$ADB" shell "mkdir -p $DEST/web $DEST/tools"
    for f in $FILES; do
        "$ADB" push -q "$SRC/$f" "$DEST/$f"
    done
    "$ADB" shell "cd $DEST && python3 -m py_compile macropad.py && rm -rf __pycache__ && python3 -c 'import json; json.load(open(\"config.json\"))'"
    echo "files pushed and checked"
}

# Restarting re-creates the USB device, which drops ADB for a few seconds,
# so the command runs detached on the device.
detached() {
    "$ADB" shell "setsid sh -c '$1' </dev/null >/dev/null 2>&1 &"
}

"$ADB" get-state >/dev/null || { echo "no device: is the Car Thing plugged in with ADB enabled?"; exit 1; }

case "${1:-}" in
    "")
        push_files
        detached "supervisorctl restart macropad"
        echo "restarting (USB reconnects in ~10 s)"
        ;;
    config)
        "$ADB" push -q "$SRC/config.json" "$DEST/config.json"
        echo "config pushed; the pad reloads it within 2 s"
        ;;
    install)
        push_files
        "$ADB" shell "mount -o remount,rw / && cp $DEST/macropad.conf /etc/supervisor.d/macropad.conf && sync; mount -o remount,ro /"
        detached "sleep 2; sync; reboot"
        echo "installed; rebooting"
        ;;
    uninstall)
        "$ADB" shell "mount -o remount,rw / && rm -f /etc/supervisor.d/macropad.conf && sync; mount -o remount,ro /"
        detached "sleep 2; sync; reboot"
        echo "boot hook removed (files stay in $DEST); rebooting to the stock UI"
        ;;
    *)
        sed -n '2,8p' "$0"; exit 2
        ;;
esac
