#!/usr/bin/env bash
# Push device/ to the Car Thing over ADB and restart the macro pad.
#
#   ./deploy.sh              copy files + restart the service
#   ./deploy.sh all          the same, then reinstall the agent on this computer (after a git pull)
#   ./deploy.sh config       copy config.json only (reloaded live, no restart)
#   ./deploy.sh install      first install: copy files + enable start on boot + reboot
#   ./deploy.sh uninstall    remove the boot hook and reboot to the stock UI
#   ./deploy.sh status       show whether the boot hook is on each system slot
#
# Works in Git Bash on Windows, Linux and macOS (its bash 3.2 too). Set ADB=/path/to/adb if adb is not on PATH.
set -euo pipefail
export MSYS_NO_PATHCONV=1          # stop Git Bash rewriting /var/... into C:/Program Files/Git/var/...

ADB=${ADB:-adb}
# Git Bash: adb.exe needs a Windows path (pwd -W); elsewhere plain pwd.
SRC="$(cd "$(dirname "$0")/device" && { pwd -W 2>/dev/null || pwd; })"
ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST=/var/macropad
CHECK=/tmp/macropad-check
FILES="macropad.py ics.py gadget.sh start.sh hook.sh macropad.conf config.json web/index.html web/shim.html tools/cdp.py"

# Check the Python files and config.json in a scratch folder first, so a broken file never
# replaces a working one. The device's adb doesn't pass exit codes back: look for CHECK_OK.
check_files() {
    "$ADB" shell "rm -rf $CHECK && mkdir -p $CHECK"
    for f in macropad.py ics.py config.json; do
        "$ADB" push -q "$SRC/$f" "$CHECK/$f"
    done
    out=$("$ADB" shell "cd $CHECK && python3 -m py_compile macropad.py ics.py && python3 -c 'import json; json.load(open(\"config.json\"))' && echo CHECK_OK; cd / && rm -rf $CHECK" 2>&1)
    case "$out" in
        *CHECK_OK*) ;;
        *) echo "$out"; echo "check failed: nothing was changed on the device"; exit 1 ;;
    esac
}

push_files() {
    check_files
    "$ADB" shell "mkdir -p $DEST/web $DEST/tools"
    for f in $FILES; do
        "$ADB" push -q "$SRC/$f" "$DEST/$f"
    done
    push_agent_versions
    "$ADB" shell "rm -rf $DEST/__pycache__"
    echo "files checked and pushed"
}

# The agents' fingerprints in this repo, so the Car Thing can tell when the agent on the
# computer is older (the agent sends the fingerprint of its own script).
fingerprint() { { sha1sum "$1" 2>/dev/null || shasum "$1"; } | cut -c1-12; }
push_agent_versions() {
    local tmp
    tmp=$(mktemp)
    printf '{"windows": "%s", "unix": "%s"}\n' "$(fingerprint "$ROOT/host/windows/nowplaying.ps1")" \
        "$(fingerprint "$ROOT/host/unix/nowplaying.py")" > "$tmp"
    "$ADB" push -q "$(cygpath -w "$tmp" 2>/dev/null || echo "$tmp")" "$DEST/agents.json"
    rm -f "$tmp"
}

# Reinstall the agent on this computer (Windows from Git Bash, Linux, macOS).
install_agent() {
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$ROOT/host/windows/install.ps1")" ;;
        Linux|Darwin) "$ROOT/host/unix/install.sh" ;;
        *) echo "unknown system $(uname -s): install the agent from host/ by hand"; return 1 ;;
    esac
}

# Restarting re-creates the USB device, which drops ADB for a few seconds,
# so the command runs detached on the device.
detached() {
    "$ADB" shell "setsid sh -c '$1' </dev/null >/dev/null 2>&1 & sleep 1"   # let it detach before adb exits
}

command -v "$ADB" >/dev/null || {
    echo "adb not found. Install Android platform-tools:"
    echo "  macOS:   brew install android-platform-tools"
    echo "  Fedora:  sudo dnf install android-tools      Debian/Ubuntu: sudo apt install adb"
    echo "  Windows: winget install Google.PlatformTools (or set ADB=/path/to/adb.exe)"
    exit 1
}
"$ADB" get-state >/dev/null || { echo "no device: is the Car Thing plugged in with ADB enabled?"; exit 1; }

case "${1:-}" in
    "")
        push_files
        detached "supervisorctl restart macropad"
        echo "restarting (USB reconnects in ~10 s)"
        ;;
    all)
        push_files
        detached "supervisorctl restart macropad"
        echo "restarting (USB reconnects in ~10 s); now the agent:"
        install_agent
        ;;
    config)
        check_files
        "$ADB" push -q "$SRC/config.json" "$DEST/config.json"
        echo "config pushed; the pad reloads it within 2 s"
        ;;
    install)
        push_files
        "$ADB" shell "sh $DEST/hook.sh install"     # both A/B system slots
        detached "sleep 2; sync; reboot"
        echo "installed; rebooting"
        ;;
    status)
        "$ADB" push -q "$SRC/hook.sh" "$DEST/hook.sh"
        "$ADB" shell "sh $DEST/hook.sh status"
        ;;
    uninstall)
        "$ADB" shell "sh $DEST/hook.sh uninstall"
        detached "sleep 2; sync; reboot"
        echo "boot hook removed (files stay in $DEST); rebooting to the stock UI"
        ;;
    *)
        sed -n '2,10p' "$0"; exit 2
        ;;
esac
