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
        # bind-hid [net]: with "net", add an RNDIS network interface (usb0) first;
        # Windows only loads RNDIS when it is the first function.
        wait_adb
        [ -e /dev/usb-ffs/hid/ep1 ] || { echo "hid function not ready"; exit 1; }
        rm -f $G/configs/c.1/rndis.usb0 $G/configs/c.1/ffs.adb $G/configs/c.1/ffs.hid
        if [ "$2" = "net" ]; then
            R=$G/functions/rndis.usb0
            mkdir -p $R
            echo 02:ca:7b:00:00:02 > $R/dev_addr 2>/dev/null
            echo 02:ca:7b:00:00:01 > $R/host_addr 2>/dev/null
            echo RNDIS > $R/os_desc/interface.rndis/compatible_id
            echo 5162001 > $R/os_desc/interface.rndis/sub_compatible_id
            ln -s $R $G/configs/c.1/rndis.usb0
            echo 0x0102 > $G/bcdDevice   # new layout: keeps Windows from reusing a cached one
        else
            echo 0x0101 > $G/bcdDevice
        fi
        ln -s $G/functions/ffs.adb $G/configs/c.1/ffs.adb
        ln -s $G/functions/ffs.hid $G/configs/c.1/ffs.hid
        echo 0x0104 > $G/idProduct
        echo "Car Thing Macro Pad" > $G/strings/0x409/product
        bind
        ;;
    adb-only)
        unbind
        rm -f $G/configs/c.1/ffs.hid $G/configs/c.1/rndis.usb0
        [ -L $G/configs/c.1/ffs.adb ] || ln -s $G/functions/ffs.adb $G/configs/c.1/ffs.adb
        echo 0x1014 > $G/idProduct
        echo 0x0100 > $G/bcdDevice
        echo Superbird > $G/strings/0x409/product
        [ -e /dev/usb-ffs/adb/ep1 ] || restart_adbd
        bind
        ;;
    *)
        echo "usage: $0 prepare|bind-hid|adb-only"; exit 2
        ;;
esac
