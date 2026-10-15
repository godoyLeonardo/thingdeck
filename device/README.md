# device/

Everything in this folder runs on the Car Thing from `/var/macropad`, which is on
the writable data partition. `../deploy.sh` copies it there.

| File | What it does |
|---|---|
| `macropad.py` | The service: USB HID over FunctionFS, dial/button input, actions, web UI server, USB network + NTP, settings |
| `gadget.sh` | Rebuilds the USB gadget (`prepare`, `bind-hid [net]`, `adb-only`) |
| `start.sh` | Supervisor entry point. Always puts USB back to ADB-only when the service exits |
| `hook.sh` | Installs/removes the boot hook on both A/B system slots (`install`, `uninstall`, `status`) |
| `macropad.conf` | Supervisor program, installed to `/etc/supervisor.d/` |
| `config.json` | Pages, tiles, presets, dial modes, hardware buttons, layout, timezone |
| `web/index.html` | Touch UI shown in the kiosk browser |
| `web/shim.html` | Bind-mounted over the stock UI's `index.html`; redirects to the service once it is up |
| `tools/cdp.py` | DevTools helper for the kiosk browser: `screenshot`, `eval`, `reload` |

## The device

Stock Spotify Buildroot (2019.02) system with root ADB enabled: kernel 4.9 on an Amlogic S905D2,
32-bit ARM userland, Python 3.7, Chromium 69 in kiosk mode under Weston, and
`supervisord` managing the services. The root filesystem is mounted read-only.
`/var` is writable and persistent, and `/tmp` (with `/etc/resolv.conf` linked
into it) is RAM.

Inputs (`/dev/input`):

| Device | Events |
|---|---|
| `gpio-keys` | `KEY_1`–`KEY_4` presets, `KEY_ESC` back, `KEY_ENTER` dial press, `KEY_M` menu |
| `rotary@0` | `REL_HWHEEL` ±1 per detent |
| `tlsc6x_dbg` | Touchscreen. Weston passes it to Chromium; the service doesn't read it |

## How it works

### USB HID without `f_hid`

This kernel has no HID (or ACM/NCM) gadget function, only RNDIS and
**FunctionFS**. The service therefore implements the HID function itself:

1. `gadget.sh prepare` unbinds the USB controller, creates `functions/ffs.hid`,
   mounts it at `/dev/usb-ffs/hid` and restarts `adbd` (which uses FunctionFS
   the same way).
2. `macropad.py` writes interface + HID class + interrupt endpoint descriptors to
   `ep0`, then answers HID class requests itself: `GET_DESCRIPTOR(Report)`,
   `SET_IDLE`, `GET_REPORT`, and so on.
3. `gadget.sh bind-hid net` links functions in the order **RNDIS → ADB → HID**
   (Windows only loads RNDIS as the first function), sets `idProduct 0x0104`, and
   binds.

Reports are written to `ep1`, one HID interface with four report IDs:

| ID | Collection | Bytes |
|---|---|---|
| 1 | Keyboard: modifiers + 6 keys (usages 0–255, so F13–F24 work) | 9 |
| 2 | Consumer control: one 16-bit usage (media keys, volume, …) | 3 |
| 3 | Mouse: 3 buttons, X, Y, wheel, AC Pan | 6 |
| 4 | Telephony Phone Mute (Linux maps it to `KEY_MICMUTE`) | 2 |

If the service dies, closing `ep0` unbinds the gadget, and `start.sh` then runs
`gadget.sh adb-only`. ADB is always reachable again. Holding the back button at
start skips the HID setup (safe mode).

### Host OS detection

When the host asks for the HID report descriptor, Windows requests 64 bytes
more than the descriptor's length, while Linux requests exactly that length.
`detect_os()` uses this on every connection. Settings can override it, and so can
tapping the status pill.

### Standby, resume and reconnect

When the HID function is not `connected` (host suspended or gone), the UI shows
the standby clock after 3 s, and hardware input is ignored. While the host is
suspended, `usb0` is taken down so no network traffic goes out.

**After resume.** The host resets the bus. `adbd` then closes and reopens its
FunctionFS files, and with configfs that unbinds the whole gadget. Nothing in the
stock system rebinds it, so the device silently drops off USB. `usb_watchdog()`
checks `UDC` every second. When it is empty it restarts `adbd`, waits until
both functions have written their descriptors, and rebinds, the same order as a
clean start. Rebinding into a half-reopened `adbd` looped; after 3 rebinds in
30 s it backs off.

**Remote wakeup does not work on this hardware, so it is not advertised**
(`bmAttributes 0x80`). Findings, so nobody has to repeat them:

- The controller is Amlogic's vendor Synopsys `dwc_otg`. Its sysfs attributes
  (`/sys/devices/platform/ff400000.dwc2_a/`: `remote_wakeup`, `regoffset`,
  `regvalue`, …) give register access. The kernel has no `/dev/mem`.
- With `bmAttributes 0xa0`, Windows arms the device, and at suspend the driver
  reports `Remote Wakeup … Enabled = 1`.
- `srp` (the `usb_gadget_wakeup()` path) did nothing. Setting
  `DCTL.RmtWkUpSig` directly cleared `DSTS.SuspSts`, but the host stayed asleep.
  The controller then stopped seeing bus resets, even after the PC woke, until a
  power cycle. The Amlogic USB2 PHY (`ffe09000.usb2phy`) is in its low-power
  "recovery tuning" state during suspend and does not drive the resume K-state.
  Its registers are not reachable from user space.
- `soft_connect` returns `EOPNOTSUPP`.

**Reconnect** (`POST /api/system {"do": "reconnect"}`, Settings → Reconnect USB,
or the standby screen) unbinds and rebinds `UDC`, a software replug. If the host
has not configured the device 20 s later, it reboots, which resets the
controller and PHY like pulling the cable.

### Kiosk takeover

At start the service bind-mounts `web/shim.html` over
`/usr/share/qt-superbird-app/webapp/index.html` and restarts Chromium. The rootfs
stays untouched, and a reboot without the service brings back the stock UI. The
UI is served from `http://127.0.0.1:8765/` and receives live updates over
Server-Sent Events.

The stock `superbird` backend is stopped only **after it has marked the boot
successful**. The bootloader keeps two system slots (`system_a`, `system_b`). Its
A/B data, at `/dev/misc` offset 2048 (`"\0AB0"`, then per slot: priority, tries
left, successful), gets "successful" cleared on every boot and one try used up.
`qt-superbird-app` sets it back about 24 s after it starts ("Boot marked as
successful" in `/var/log/superbird.info`). If nothing marks 7 boots in a row,
the bootloader falls back to the other slot. Stopping the app too early caused
exactly that, and the boot hook on the old slot was gone with it. So
`wait_for_boot_marked_successful()` waits for that log line (90 s at most), and
`hook.sh` installs the hook on both slots.

### Theme

`web/index.html` defines each theme as CSS variables under
`:root[data-theme=…]`. Settings saves `theme` and `accent` in `state.json`, and
the page sets `data-theme` and the `--acc`/`--acc-soft` accent variables.
Accent `theme` uses the color scheme's own accent; the fixed accents have a
lighter and a darker shade for dark and light themes.

### Clock and timezone

The Car Thing has no RTC and boots with its clock at 2015-01-01. Through the RNDIS link
(`usb0`), `udhcpc` gets an address from the computer's internet sharing (Windows
ICS or NetworkManager "shared"). Then `ntpd -q` sets the clock
(`pool.ntp.org`, `time.cloudflare.com`, or `162.159.200.1`, which needs no DNS).
It re-syncs every hour, and `udhcpc` is restarted every 30 s until it gets a lease.

This Chromium build has **no `Intl`**, and the device has no zoneinfo database. So
the timezone is a POSIX `TZ` rule in `config.json` (`tz`), applied with
`time.tzset()`. The service sends the current UTC offset to the page and
re-publishes it when DST switches. `timezone` is only the display name. To
get the rule for another zone, take the last line of its tzdata file (on Linux:
`tail -1 /usr/share/zoneinfo/Europe/Madrid`).

### Brightness

`/sys/class/backlight/aml-bl/brightness` is **inverted**: 0–255, and higher means
dimmer. Auto mode keeps the stock `sp-als-backlight` light-sensor service running.
Manual mode stops it and writes `260 - level`.

## config.json

Top level:

| Key | Meaning |
|---|---|
| `layout` | Host keyboard layout for typed text: `latam` or `us` (Settings can override) |
| `default_os` | Used until the host OS is detected: `windows` or `linux` |
| `timezone`, `tz` | Display name and POSIX TZ rule (see above) |
| `usb_network` | `false` leaves out the RNDIS interface (no clock sync) |
| `kiosk` | `false` keeps the stock UI |
| `grid` | Tile grid `[columns, rows]`, default `[4, 2]`; pages can override it |
| `hardware` | `knob`, `back`, `menu`: `{"tap": action, "hold": action}`; `dial_invert` |
| `dial_modes` | `[{"name", "cw", "ccw", "press"}]` |
| `presets` | Default 4 top-button actions |
| `pages` | `[{"name", "presets"?, "grid"?, "tiles": [...]}]` |

Tiles and presets are actions plus `label`, optional `icon` (emoji), `hint`
(small text; generated if missing) and `color`.

### Actions

| Action | Example |
|---|---|
| Key combo | `{"key": "ctrl+shift+p"}`. Modifiers: `ctrl shift alt win` (`cmd`/`super` too), `rctrl rshift ralt/altgr rwin`. Keys: letters, digits, `f1`–`f24`, `enter esc tab space backspace delete insert home end pageup pagedown up down left right printscreen menu`, `plus`, or any single character of the active layout (`ctrl+ñ`, `win+.`) |
| Media key | `{"media": "play_pause"}`: `next prev stop mute vol_up vol_down calculator explorer mail browser browser_back browser_forward browser_refresh search brightness_up brightness_down`, or a raw consumer usage number |
| Type text | `{"type": "git status\n"}` (translated for the layout; ñ, accents and dead keys handled) |
| Sequence | `{"seq": [{"key": "win"}, {"type": "files\n"}], "delay": 0.5}` |
| Wheel | `{"wheel": -1}`, `{"hwheel": 1}`, with modifiers: `{"wheel": 1, "mods": "ctrl"}` |
| Click | `{"click": "middle"}` (`left`, `right`, `middle`) |
| Mic mute | `{"mic_mute": true}` (HID telephony; system-wide on Linux) |
| Page / dial mode | `{"page": "next"}`, `{"page": "Code"}`, `{"page": 0}`; `{"dial": "next"}`, `{"dial": "Volume"}` |
| Dial press | `{"dial_press": true}` (runs the current dial mode's `press`) |
| Notification | `{"notify": "hello", "level": "ok"}` |

**Per-OS overrides:** any action can carry a `windows` and/or `linux` object.
When the host matches, it replaces the action, keeping `label`/`icon`/`color`
unless the override sets them:

```json
{ "label": "Task view", "key": "win+tab", "linux": { "label": "Overview", "key": "win" } }
```

## HTTP API

Served on port 8765. From the computer over the USB network, only `ping` and
`notify` are allowed; everything else is restricted to the device itself.

| Method | Path | Body | Purpose |
|---|---|---|---|
| GET | `/api/ping` | | Health check |
| POST | `/api/notify` | `{"text", "level"}` | Toast on screen |
| GET | `/api/state` | | Config, state, presets |
| GET | `/api/events` | | SSE stream: `state`, `config`, `fired`, `turn`, `hw`, `toast` |
| GET | `/api/info` | | Uptime, temperature, memory, load, network |
| POST | `/api/tap` | `{"page", "index"}` | Run a tile |
| POST | `/api/preset` | `{"index"}` | Run a preset |
| POST | `/api/page`, `/api/dial`, `/api/os` | `{"page"}` / `{"dial"}` / `{"os"}` | Switch page / dial mode / host OS |
| POST | `/api/settings` | `{"brightness": 200, …}` | Change settings (saved in `state.json`) |
| POST | `/api/run` | any action | Run an action (local only) |
| POST | `/api/system` | `{"do": "reconnect" \| "restart" \| "reboot"}` | Reconnect USB (software replug) / restart service / reboot |

## Debugging

```bash
adb shell tail -f /var/log/macropad.log         # service log
adb shell supervisorctl status                  # all services
adb shell python3 /var/macropad/tools/cdp.py screenshot /tmp/s.png && adb pull /tmp/s.png
adb shell python3 /var/macropad/tools/cdp.py eval "JSON.stringify(st)"
adb shell curl -s http://127.0.0.1:8765/api/state
```

Restarting the service re-creates the USB device, which also drops ADB. Run it
detached (`deploy.sh` does this) or the restart is cut off mid-way:

```bash
adb shell 'setsid sh -c "supervisorctl restart macropad" </dev/null >/dev/null 2>&1 &'
```
