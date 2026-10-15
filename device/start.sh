#!/bin/sh
# Supervisor entry point. Whatever happens to macropad.py, put the USB gadget
# back to plain ADB when it exits so the device never ends up unreachable.
cd /var/macropad
python3 -u macropad.py "$@" &
PID=$!
trap 'kill -TERM $PID 2>/dev/null' TERM INT
wait $PID
wait $PID
RC=$?
sh /var/macropad/gadget.sh adb-only
exit $RC
