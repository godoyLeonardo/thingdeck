# device/

Everything in this folder runs on the Car Thing from `/var/macropad`, which is on
the writable data partition. `../deploy.sh` copies it there.

| File | What it does |
|---|---|
| `macropad.py` | The service: USB HID over FunctionFS, dial and button input, actions, settings, and the local web UI server |
| `gadget.sh` | Rebuilds the USB gadget (`prepare`, `bind-hid`, `adb-only`) |
| `start.sh` | Supervisor entry point. Always puts USB back to ADB-only when the service exits |
| `hook.sh` | Installs/removes the boot hook on both A/B system slots (`install`, `uninstall`, `status`) |
| `macropad.conf` | Supervisor program, installed to `/etc/supervisor.d/` |
| `config.json` | Pages, tiles, presets, dial modes, hardware buttons, layout, OS defaults |
| `web/index.html` | Touch UI shown in the kiosk browser |
| `web/shim.html` | Bind-mounted over the stock UI's `index.html`; redirects to the service once it is up |
| `tools/cdp.py` | DevTools helper for the kiosk browser: `screenshot`, `eval`, `reload` |

## The device

Stock Spotify Buildroot (2019.02) system with root ADB enabled: kernel 4.9 on an
Amlogic S905D2, 32-bit ARM userland, Python 3.7, and Chromium 69 in kiosk mode
under Weston, with `supervisord` managing the services. The root filesystem is
mounted read-only, `/var` is writable and persistent, and `/tmp` is RAM.

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
3. `gadget.sh bind-hid` links ADB and HID into the configuration, sets
   `idProduct 0x0104`, and binds.

Reports are written to `ep1`, one HID interface with four report IDs:

| ID | Collection | Bytes |
|---|---|---|
| 1 | Keyboard: modifiers + 6 keys (usages 0–255, so F13–F24 work) | 9 |
| 2 | Consumer control: one 16-bit usage (media keys, volume, …) | 3 |
| 3 | Mouse: 3 buttons, X, Y, wheel, AC Pan | 6 |
| 4 | Telephony Phone Mute (Linux maps it to `KEY_MICMUTE`) | 2 |

All three OSes drive this with their built-in HID drivers. If the service dies,
closing `ep0` unbinds the gadget, and `start.sh` then runs `gadget.sh adb-only`,
so ADB is always reachable again. Holding the back button at start skips the HID
setup (safe mode).

### Host OS

When the host asks for the HID report descriptor, Windows requests 64 bytes more
than the descriptor's length. Linux and macOS request exactly that length, so
they can't be told apart this way. `detect_os()` reports `windows` or `other`,
and `host_os()` resolves it:

1. The Settings choice (`auto`, `windows`, `mac`, `linux`) wins if it isn't `auto`.
2. Detected `windows` → `windows`.
3. Detected `other` → `config.other_os` (default `linux`).
4. Nothing detected yet → `config.default_os`.

The result picks per-OS action overrides, and it can pick a per-OS layout
(`layout_mac`, `layout_linux`, …) when Settings doesn't set one.

### Standby, resume and reconnect

When the HID function is not `connected` (host suspended or gone), the UI shows
the standby screen after 3 s, and hardware input is ignored. After the same 3 s
the backlight drops to its minimum (`BRIGHTNESS_MIN`, with the light-sensor service
stopped); the brightness setting is applied again when the host is back.

**After resume.** The host resets the bus. `adbd` then closes and reopens its
FunctionFS files, and with configfs that unbinds the whole gadget. Nothing in the
stock system rebinds it, so the device silently drops off USB. `usb_watchdog()`
checks `UDC` every second. When it is empty, it restarts `adbd`, waits until both
functions have written their descriptors, and rebinds, in the same order as a
clean start. Rebinding into a half-reopened `adbd` looped, so after 3 rebinds in
30 s it backs off.

**Reconnect** (`POST /api/system {"do": "reconnect"}`, Settings → Reconnect USB,
or the standby screen) unbinds `UDC` and lets the watchdog rebind it: a software
replug. If the host has not configured the device 20 s later, the service
reboots, which resets the controller and PHY like pulling the cable.

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
- A mainline kernel (for example [yocto-superbird](https://github.com/JoeyEamigh/yocto-superbird))
  has a different PHY driver and might behave differently. That's untested.

### Kiosk takeover and the A/B boot slots

At start the service bind-mounts `web/shim.html` over
`/usr/share/qt-superbird-app/webapp/index.html` and restarts Chromium. The rootfs
stays untouched, and a reboot without the service brings back the stock UI. The
UI is served from `http://127.0.0.1:8765/` (local only) and gets live updates
over Server-Sent Events.

The stock `superbird` backend is stopped only **after it has marked the boot
successful**. The bootloader keeps two system slots (`system_a`, `system_b`). Its
A/B data, at `/dev/misc` offset 2048 (`"\0AB0"`, then per slot: priority, tries
left, successful), gets "successful" cleared and one try used up on every boot.
`qt-superbird-app` sets it back about 25 s after it starts ("Boot marked as
successful" in `/var/log/superbird.info`). If nothing marks 7 boots in a row,
the bootloader falls back to the other slot. Stopping the app too early caused
exactly that, and the boot hook on the old slot was gone with it. So
`wait_for_boot_marked_successful()` polls the running slot's flag in `/dev/misc`
(120 s at most; the log can't be trusted because it rotates at 1 MB), and
`hook.sh` installs the hook on both slots.

### Clock and lock screen

The host agent posts a heartbeat to `POST /api/host` on every change and every
10 s: `time_ms`, `utc_offset_min` and `locked`. `HostLink` sets the system clock
with `clock_settime` when it is more than 2 s off (the device has no RTC; the
clock then keeps running while the computer sleeps) and keeps the UTC offset,
which the UI uses for local time (this Chromium has no `Intl`). A computer counts
as locked only while heartbeats say so; with none for 30 s, the lock is dropped.

While locked, `Pad.run()` refuses any action that would press keys (`key`, `type`,
wheel with `mods`, sequences or dial presses containing those): on a lock screen
they would land in the password box. Media keys, volume, plain wheel, clicks and
mic mute still work. Lock detection: `LogonUI` running (Windows), the
freedesktop or GNOME screensaver `GetActive` over D-Bus (Linux),
`CGSSessionScreenIsLocked` (macOS).

### Now playing

A host agent (`../host/`) posts the current track to `POST /api/nowplaying` through
`adb forward tcp:28765 tcp:8765`, so it arrives as a local request. `NowPlaying`
keeps the metadata and the cover separately: the cover comes base64 with an
`art_key` (a hash of title, artist, album and app) only when the key changes, or
when the reply asks for it (`need_art`, e.g. after a service restart). Position
updates every 2 s stay small. The UI gets `nowplaying` events over SSE, loads the
cover from `/api/nowplaying/art?k=…`, and moves the progress bar between updates.
With no update for 15 s the data is dropped (computer off, agent stopped).

A page with `"header": "clock"` (Media, by default) shows a big clock and the date
in place of the preset labels and dial chip, as long as the agent has set the
time; the presets and dial still work. A page with `"nowplaying": true` (Media, by default) switches to the now-playing
layout while something plays: cover, details and progress on top, and its tiles
as a compact row of controls below.

### Themes

`web/index.html` defines each theme as CSS variables under
`:root[data-theme=…]`. Settings saves `theme` and `accent` in `state.json`, and
the page sets `data-theme` and the `--acc`/`--acc-soft` accent variables. Accent
`theme` uses the color scheme's own accent; the fixed accents have a lighter and
a darker shade for dark and light themes.

### Brightness

`/sys/class/backlight/aml-bl/brightness` is **inverted**: 0–255, and higher means
dimmer. Auto mode keeps the stock `sp-als-backlight` light-sensor service running.
Manual mode stops it and writes `260 - level`.

## config.json

Top level:

| Key | Meaning |
|---|---|
| `layout` | Host keyboard layout for typed text: `latam` or `us`. Settings can override it; `layout_<os>` sets one per OS |
| `default_os` | Used until the host OS is detected: `windows`, `mac` or `linux` |
| `other_os` | What a non-Windows host is taken to be: `linux` (default) or `mac` |
| `icon_style` | `solid`, `outline` or `emoji` for `symbol` icons (Settings → Appearance can override it) |
| `icon_color` | `neutral` (theme text color), `accent` or `category` (per action type) for `symbol` icons; Settings can override it |
| `kiosk` | `false` keeps the stock UI |
| `grid` | Tile grid `[columns, rows]`, default `[4, 2]`; pages can override it |
| `hardware` | `knob`, `back`, `menu`: `{"tap": action, "hold": action}`; `dial_invert` |
| `dial_modes` | `[{"name", "cw", "ccw", "press"}]` |
| `presets` | Default 4 top-button actions |
| `pages` | `[{"name", "presets"?, "grid"?, "nowplaying"?, "header"?, "tiles": [...]}]`, or a choice slot (below) |

Tiles and presets are actions plus `label`, optional `icon` (an emoji) or `symbol`
(a built-in icon: `play`, `pause`, `prev`, `next`, `stop`, `mute`, `note`, drawn in
the current icon style), `hint` (small text; generated if missing) and `color`. In
the media card's control row, `"wide": true` makes a tile double width.

### Actions

| Action | Example |
|---|---|
| Key combo | `{"key": "ctrl+shift+p"}`. Modifiers: `ctrl shift alt win`, with `cmd`/`super` = `win` and `option` = `alt`, plus `rctrl rshift ralt/altgr rwin`. Keys: letters, digits, `f1`–`f24`, `enter esc tab space backspace delete insert home end pageup pagedown up down left right printscreen menu equal minus plus`, or any single character of the active layout (`ctrl+ñ`, `win+.`) |
| Media key | `{"media": "play_pause"}`: `next prev stop mute vol_up vol_down calculator explorer mail browser browser_back browser_forward browser_refresh search brightness_up brightness_down`, or a raw consumer usage number |
| Type text | `{"type": "git status\n"}` (translated for the layout; ñ, accents and dead keys handled) |
| Sequence | `{"seq": [{"key": "win"}, {"type": "files\n"}], "delay": 0.5}` |
| Wheel | `{"wheel": -1}`, `{"hwheel": 1}`, with modifiers: `{"wheel": 1, "mods": "ctrl"}` |
| Click | `{"click": "middle"}` (`left`, `right`, `middle`) |
| Mic mute | `{"mic_mute": true}` (HID telephony; system-wide on Linux) |
| Page / dial mode | `{"page": "next"}`, `{"page": "Code"}`, `{"page": 0}`; `{"dial": "next"}`, `{"dial": "Volume"}` |
| Dial press | `{"dial_press": true}` (runs the current dial mode's `press`) |
| Message | `{"notify": "hello", "level": "ok"}` (shows a toast on the screen) |

**Per-OS overrides:** any action can carry `windows`, `mac` and/or `linux`
objects. When the host matches, the override replaces the action, keeping
`label`/`icon`/`symbol`/`color` unless it sets them. This works inside sequences too:

```json
{ "label": "Task view", "key": "win+tab",
  "mac": { "label": "Mission Control", "key": "ctrl+up" },
  "linux": { "label": "Overview", "key": "win" } }
```

**Choice slots:** a page entry with `choices` holds alternative pages for one
tab. Settings → General → Pages picks one (saved as `page_choices` in
`state.json`). "Auto" uses `default`, which can be a name or a per-OS map with an
`other` fallback:

```json
{ "id": "calls", "label": "Calls page",
  "default": { "windows": "Discord", "other": "Meetings" },
  "choices": [ { "name": "Meetings", "tiles": [...] }, { "name": "Discord", "tiles": [...] } ] }
```

## HTTP API

Served on `127.0.0.1:8765`, reachable only from the Car Thing itself (the kiosk
page, or `adb shell`).

| Method | Path | Body | Purpose |
|---|---|---|---|
| GET | `/api/ping` | | Health check (used by `shim.html`) |
| GET | `/api/state` | | Config, state, presets |
| GET | `/api/events` | | SSE stream: `state`, `config`, `fired`, `turn`, `hw`, `toast` |
| GET | `/api/info` | | Uptime, temperature, memory, load, brightness |
| POST | `/api/tap` | `{"page", "index"}` | Run a tile |
| POST | `/api/preset` | `{"index"}` | Run a preset |
| POST | `/api/page`, `/api/dial`, `/api/os` | `{"page"}` / `{"dial"}` / `{"os"}` | Switch page / dial mode / host OS |
| POST | `/api/settings` | `{"theme": "nord", …}` | Change settings (saved in `state.json`) |
| POST | `/api/host` | `{"time_ms", "utc_offset_min", "locked"}` | Heartbeat from the host agent: sets the clock, lock state |
| POST | `/api/nowplaying` | `{"title", "artist", "album", "album_artist", "track_number", "app", "playing", "position_ms", "duration_ms", "shuffle", "repeat", "art_key", "art"?, "art_type"?}` | From the host agent; `{"title": null}` clears. Replies `{"need_art": bool}` |
| GET | `/api/nowplaying`, `/api/nowplaying/art` | | Current track metadata; its cover image |
| POST | `/api/run` | any action | Run an action |
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
detached (`deploy.sh` does this) or the restart gets cut off midway:

```bash
adb shell 'setsid sh -c "supervisorctl restart macropad" </dev/null >/dev/null 2>&1 &'
```
