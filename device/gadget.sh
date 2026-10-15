#!/bin/sh
# USB gadget control for the macro pad.
#   prepare   unbind, create + mount the ffs.hid function, restart adbd
#   bind-hid  link ffs.hid next to adb and bind (macropad.py must have written its descriptors)
#   adb-only  back to the stock ADB-only gadget (always safe to run)

G=/sys/kernel/config/usb_gadget/g1
UDC_NAME=$(ls -1 /sys/class/udc/ | head -n1)

unbind() {
    if [ -n "$(cat $G/UDC)" ]; then echo "" > $G/UDC; fi
    sleep 1
}

bind() {
    echo "$UDC_NAME" > $G/UDC
}

wait_adb() {
    i=0
    while [ ! -e /dev/usb-ffs/adb/ep1 ] && [ $i -lt 15 ]; do sleep 1; i=$((i+1)); done
}

restart_adbd() {
    killall adbd 2>/dev/null
    sleep 1
    setsid /usr/bin/adbd </dev/null >/dev/null 2>&1 &
    wait_adb
}

case "$1" in
    prepare)
        unbind
        mkdir -p $G/functions/ffs.hid /dev/usb-ffs/hid
        grep -q " /dev/usb-ffs/hid " /proc/mounts || mount -t functionfs hid /dev/usb-ffs/hid
        restart_adbd
        ;;
    bind-hid)
        wait_adb
        [ -e /dev/usb-ffs/hid/ep1 ] || { echo "hid function not ready"; exit 1; }
        # Exactly two functions, ADB then HID; drop anything else linked earlier.
        for l in $G/configs/c.1/*; do
            [ -L "$l" ] && [ "${l##*/}" != ffs.adb ] && rm -f "$l"
        done
        [ -L $G/configs/c.1/ffs.adb ] || ln -s $G/functions/ffs.adb $G/configs/c.1/ffs.adb
        ln -s $G/functions/ffs.hid $G/configs/c.1/ffs.hid
        echo 0x80 > $G/configs/c.1/bmAttributes   # bus powered, no remote wakeup (this PHY cannot send it)
        echo 0x0104 > $G/idProduct
        echo 0x0107 > $G/bcdDevice                # new value per layout change, so hosts don't reuse a cached one
        echo "Car Thing Macro Pad" > $G/strings/0x409/product
        bind
        ;;
    adb-only)
        unbind
        for l in $G/configs/c.1/*; do
            [ -L "$l" ] && [ "${l##*/}" != ffs.adb ] && rm -f "$l"
        done
        [ -L $G/configs/c.1/ffs.adb ] || ln -s $G/functions/ffs.adb $G/configs/c.1/ffs.adb
        echo 0x1014 > $G/idProduct
        echo 0x0100 > $G/bcdDevice
        echo 0x80 > $G/configs/c.1/bmAttributes
        echo Superbird > $G/strings/0x409/product
        [ -e /dev/usb-ffs/adb/ep1 ] || restart_adbd
        bind
        ;;
    *)
        echo "usage: $0 prepare|bind-hid|adb-only"; exit 2
        ;;
esac
