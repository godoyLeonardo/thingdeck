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

- **Windows:**
  1. Make internet sharing survive reboots. By default the sharing service is
     set to Manual and does not come back after a restart, so the clock stops
     syncing. Open an **admin** Command Prompt or PowerShell (right-click Start →
     Terminal (Admin)) and run:

     ```cmd
     sc.exe config SharedAccess start= auto
     ```

     ```cmd
     reg add "HKLM\Software\Microsoft\Windows\CurrentVersion\SharedAccess" /v EnableRebootPersistConnection /t REG_DWORD /d 1 /f
     ```

     ```cmd
     net start SharedAccess
     ```

     Keep the space after `start=`. Write `sc.exe`, not `sc`: in PowerShell,
     `sc` means something else.
  2. Turn sharing on: press Win+R, run `ncpa.cpl`. Right-click your internet
     adapter (not the Car Thing one) → Properties → Sharing. Check "Allow other
     network users to connect…" and pick the Car Thing adapter ("Remote NDIS
     Compatible Device") as the home network.

  If the clock ever stops syncing (for example after Windows gives the Car Thing
  adapter a new name), turn that checkbox off and on again.
- **Linux (NetworkManager, e.g. Fedora):**

  ```bash
  nmcli connection add type ethernet ifname enx02ca7b000001 con-name carthing ipv4.method shared ipv6.method ignore
  ```

  The interface name stays the same because the Car Thing uses a fixed MAC address.

The timezone is set in `device/config.json` (`timezone` for display, `tz` for the
POSIX rule actually used; see [device/README.md](device/README.md#clock-and-timezone)).

## When the computer is off

When the computer sleeps or shuts down, and the Car Thing still has power, the
screen switches to a standby view: a large clock and the date. The buttons do
nothing until the computer is back. When the computer wakes up, the Car Thing
reconnects by itself and the normal screen returns.

The clock keeps running from the last sync while the computer is off. It can
drift a little until the next sync.

**The Car Thing cannot turn the computer on.** Waking from USB was tried and
does not work on this hardware:

- **From sleep:** Windows arms the device and enables remote wakeup, and the
  USB controller accepts the request. But the Amlogic USB PHY stays in
  low-power mode while the bus is suspended, so the wake signal never reaches the
  wire. Forcing it also locked up the controller until a power cycle.
- **From shutdown:** many boards, Gigabyte B450 included, power-cycle USB at
  shutdown even with ErP disabled. The Car Thing reboots, and a device the
  computer never configured is not allowed to wake it.

Wake the computer with its keyboard or power button.

**If it ever gets stuck** on the standby screen while the computer is on, tap
**Reconnect** at the bottom of the standby screen, or use **Reconnect USB** in
Settings. That detaches and reattaches USB, like pulling the cable. If the
computer still has not configured the Car Thing 15 seconds later, the Car Thing
reboots, which resets its USB hardware like a real replug.

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
