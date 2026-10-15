# streamthing

Turns a Spotify Car Thing into a USB macro pad and media controller, a small
Stream Deck. It needs **no app on the computer**. The computer sees a standard
USB keyboard, media keys and mouse wheel, so it works on Windows and Linux as soon
as you plug it in.

- **Touchscreen:** pages of large tiles (Media, Desktop, Code, Shell, Meetings),
  each sending a shortcut, a media key, typed text or a sequence of steps.
- **4 top buttons:** presets that change with the page. Labels are shown on the
  screen right under the buttons.
- **Dial:** volume, scroll, zoom, scrub (arrow keys), browser tabs, undo/redo.
  The back button switches dial mode.
- **Windows / Linux aware:** the pad detects which OS it is plugged into and uses
  that OS's shortcuts.
- **Keyboard layouts:** typed text is translated for the host layout
  (Latin American and US are included).
- **Clock:** synced from internet time servers over the same USB cable.
- **Settings tab:** brightness (auto light sensor or manual), host OS, layout,
  dial direction, 24h/12h clock, device stats, restart/reboot.
- **Notifications:** scripts on the computer can show a message on the screen
  with `curl`.

Everything runs on the Car Thing itself: a Python service plus a page in its
built-in browser. Details are in [device/README.md](device/README.md).

## Requirements

- A Car Thing running the stock Spotify system with **ADB enabled** (for example
  the "adb enabled" stock image flashed with `superbird-tool`). It must show up in
  `adb devices`.
- `adb` on the computer you deploy from (Android platform-tools).
- Bash to run `deploy.sh`: Git Bash on Windows, or any Linux shell.

## Install

```bash
./deploy.sh install
```

This copies `device/` to `/var/macropad` on the Car Thing, then adds one file,
`/etc/supervisor.d/macropad.conf`, so the pad starts on boot, and reboots.
No stock file is changed.

After editing code:

```bash
./deploy.sh
```

After editing only `device/config.json` (no restart; the pad reloads it within 2 s):

```bash
./deploy.sh config
```

To go back to the stock UI (the files stay in `/var/macropad`):

```bash
./deploy.sh uninstall
```

## Hardware controls

| Control | Default |
|---|---|
| Buttons 1–4 (top edge) | Page presets (media keys on most pages) |
| Dial turn | Current dial mode (Volume, Scroll, Zoom, Scrub, Tabs, Undo/Redo) |
| Dial press | Dial mode action (mute, middle click, reset zoom, …) |
| Dial hold | Play/Pause |
| Back button | Next dial mode · hold: back to Volume |
| Menu button (M) | Next page · hold: first page |
| Swipe left/right on the tiles | Change page |
| Tap the status pill (bottom right) | Cycle host OS: Auto → Windows → Linux |

All of these can be changed in `device/config.json`.

## Host setup

**Keys, media keys and wheel:** nothing to set up, on any OS.

**Clock (one time per computer):** the Car Thing has no battery-backed clock. It
gets the time over a small network link on the USB cable, which the computer has
to share its internet connection with. Until then the clock shows `--:--`.

- **Windows:** press Win+R, run `ncpa.cpl`. Right-click your internet adapter
  (not the Car Thing one) → Properties → Sharing. Check "Allow other network
  users to connect…" and pick the Car Thing adapter ("Remote NDIS Compatible
  Device") as the home network. Windows sometimes drops this after a PC reboot;
  if the clock stops syncing, turn the checkbox off and on.
- **Linux (NetworkManager, e.g. Fedora):**

  ```bash
  nmcli connection add type ethernet ifname enx02ca7b000001 con-name carthing ipv4.method shared ipv6.method ignore
  ```

  The interface name stays the same because the Car Thing uses a fixed MAC address.

The timezone is set in `device/config.json` (`timezone` for display, `tz` for the
POSIX rule actually used; see [device/README.md](device/README.md#clock-and-timezone)).

## Notifications from scripts

Once the network link is up, the Car Thing's address is shown in Settings → Device
(on Windows it is in `192.168.137.x`):

```bash
curl -d '{"text": "build passed", "level": "ok"}' http://192.168.137.115:8765/api/notify
```

`level` is `info`, `ok`, `warn` or `fail`. Over the network only notifications are
accepted. Keystrokes can only be triggered from the device itself.

## Customizing

Edit `device/config.json` and run `./deploy.sh config`. A tile looks like:

```json
{ "label": "Snip", "icon": "✂️", "key": "win+shift+s", "linux": { "key": "printscreen" } }
```

The full action reference (keys, media, typed text, sequences, wheel, page/dial
switching, per-OS overrides) is in [device/README.md](device/README.md#configjson).

## Recovery

- If the service crashes, the USB device falls back to plain ADB, so you can
  always reach the device again.
- **Safe mode:** hold the back button while the Car Thing boots. The pad then
  leaves USB as ADB only.
- `./deploy.sh uninstall` removes the boot hook and restores the stock UI.

## Known limitations

- Windows has no system-wide mic-mute key, so "Mic toggle" sends Win+Alt+K. That
  works in Teams and other apps that support Windows 11's call-mute shortcut. On
  Linux it is a real mic-mute key.
- The Linux shortcut overrides assume GNOME (Fedora Workstation, Ubuntu). On KDE
  a few desktop shortcuts need adjusting.
- Typed text assumes the host uses the layout chosen in Settings.
