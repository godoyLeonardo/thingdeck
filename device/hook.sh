#!/bin/sh
# Install or remove the boot hook (/etc/supervisor.d/macropad.conf) on BOTH system slots.
#   sh hook.sh install | uninstall | status
#
# The bootloader keeps two system partitions (A/B) and can switch between them (after
# 7 boots that were never marked successful, for example). With the hook on only one
# slot, a switch silently brings back the stock UI.

CONF=/var/macropad/macropad.conf
HOOK=etc/supervisor.d/macropad.conf
CUR=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^androidboot.slot_suffix=_//p')   # a or b

# run_on_slot SLOT CMD: CMD gets the slot's root directory as $1, mounted read-write
run_on_slot() {
    if [ "$1" = "$CUR" ]; then
        mount -o remount,rw / && "$2" / ; rc=$?
        sync; mount -o remount,ro /
    else
        mkdir -p /tmp/slot_$1
        mount -t ext4 /dev/system_$1 /tmp/slot_$1 && "$2" /tmp/slot_$1 ; rc=$?
        sync; umount /tmp/slot_$1 2>/dev/null; rmdir /tmp/slot_$1 2>/dev/null
    fi
    return $rc
}

do_install()   { cp "$CONF" "$1/$HOOK"; }
do_uninstall() { rm -f "$1/$HOOK"; }
do_status()    { [ -f "$1/$HOOK" ] && echo installed || echo missing; }

case "$1" in
    install|uninstall|status)
        for s in a b; do
            out=$(run_on_slot $s do_$1) || { echo "slot $s: $1 failed"; exit 1; }
            echo "slot $s$([ $s = "$CUR" ] && echo ' (running)'): ${out:-$1 ok}"
        done
        ;;
    *)
        echo "usage: $0 install|uninstall|status"; exit 2
        ;;
esac
