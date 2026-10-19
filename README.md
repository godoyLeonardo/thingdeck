# streamthing

Turns a Spotify Car Thing into a USB macro pad and media controller, a small
Stream Deck. It needs **no app on the computer**. The computer sees a standard
USB keyboard, media keys and mouse wheel, so it works on Windows, macOS and Linux
as soon as you plug it in.

- **Touchscreen:** pages of large tiles (Media, Desktop, Code, Shell, and Discord
  on Windows or Meetings on macOS/Linux), each sending a shortcut, a media key, typed text or a sequence of steps.
- **4 top buttons:** presets that change with the page. Labels are shown on the
  screen right under the buttons.
- **Dial:** volume, scroll, zoom, scrub (arrow keys), browser tabs, undo/redo.
  The back button switches dial mode.
- **Windows, macOS and Linux:** each tile can have per-OS shortcuts. The pad
  detects Windows by itself; pick macOS or Linux once in Settings (see
  [Host OS](#host-os)).
- **Keyboard layouts:** typed text is translated for the host layout
  (Latin American and US are included).
- **Settings:** brightness (light sensor or manual), host OS, layout, dial
  direction, themes, device info, and Reconnect / Restart / Reboot.
- **Themes:** Dark, Black, Light, and the editor color schemes Nord, Dracula,
  Catppuccin, Gruvbox, Tokyo Night and Solarized, each with its own accent or
  one of six accent colors.
- **Standby screen** while the computer sleeps; the pad reconnects by itself
  when it wakes up.

Everything runs on the Car Thing itself: a Python service plus a page in its
built-in browser. How it works is in [device/README.md](device/README.md).

## Requirements

- A Car Thing running the stock Spotify system with **ADB enabled** (for example
  the "adb enabled" stock image flashed with `superbird-tool`). It must show up in
  `adb devices`.
- `adb` (Android platform-tools) on the computer you deploy from:

  | OS | Install |
  |---|---|
  | macOS | `brew install android-platform-tools` |
  | Fedora | `sudo dnf install android-tools` |
  | Debian / Ubuntu | `sudo apt install adb` |
  | Windows | `winget install Google.PlatformTools` |

- Bash for `deploy.sh`: the built-in one on macOS and Linux, Git Bash on Windows.

Deploying is the only step that needs `adb`. Once installed, the pad works on any
computer without it.

## Install

```bash
./deploy.sh install
```

This copies `device/` to `/var/macropad` on the Car Thing, adds one file,
`/etc/supervisor.d/macropad.conf`, so the pad starts on boot, and reboots. No
stock file is changed. The file goes on **both** system partitions: the
bootloader keeps two copies of the system (slots A and B) and can switch between
them.

| Command | Does |
|---|---|
| `./deploy.sh` | Push the files and restart the service (after editing code) |
| `./deploy.sh config` | Push only `device/config.json`; the pad reloads it within 2 s |
| `./deploy.sh status` | Show whether the boot hook is on each system slot |
| `./deploy.sh uninstall` | Remove the boot hook and reboot to the stock UI (files stay in `/var/macropad`) |

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
| Tap the status pill (bottom right) | Cycle host OS: Auto → Windows → macOS → Linux |

All of these can be changed in `device/config.json`.

## Host OS

Shortcuts differ per OS, so tiles, and whole pages, can carry `"windows"`,
`"mac"` and `"linux"` overrides. On Windows the Meetings page is replaced by a
**Discord** page (mute, deafen, answer/decline, quick switcher, …), using
Discord's default shortcuts. Mute and deafen work even when Discord isn't
focused. The
defaults use standard shortcuts: Mission Control and Spotlight on macOS,
GNOME's on Linux.

Over USB, Windows identifies itself, but macOS and Linux look the same. So:

- **Windows:** detected automatically.
- **Linux:** used automatically when the host isn't Windows (`"other_os": "linux"`
  in `config.json`).
- **macOS:** choose **macOS** under Settings → General → Host OS, or by tapping
  the status pill. On a Mac, shortcut hints are shown as ⌃⇧⌥⌘.

The choice is saved on the Car Thing. If you move between a Mac and a Linux
machine, switch it in Settings.

## Keyboard layout

The pad sends key *positions*, so typed text (the Shell tiles, for example) has
to match the layout the computer uses. Set it under Settings → General → Keyboard
layout:

- **Latin American:** matches Windows "Latin American" and Linux `latam`.
- **US:** use this on a Mac. macOS's own "Latin American" layout puts symbols in
  different places.

Shortcuts made of letters, digits and named keys work with any layout.

## When the computer sleeps

When the computer sleeps or shuts down and the Car Thing still has power, the
screen shows **Computer is asleep**, dims to its minimum brightness, and the
buttons do nothing. Your brightness setting comes back with the computer. When the computer
wakes up, the Car Thing reconnects by itself within a few seconds.

**The Car Thing cannot wake or turn on the computer.** Waking over USB was tried,
and the hardware can't do it. Details are in
[device/README.md](device/README.md#standby-resume-and-reconnect). Use the
computer's own keyboard or power button.

If it ever stays on the standby screen while the computer is on, tap **Reconnect**
at the bottom of that screen (or Settings → Device → Reconnect USB). That
detaches and reattaches USB, like pulling the cable. If the computer still hasn't
picked it up after 20 seconds, the Car Thing reboots.

## Customizing

Edit `device/config.json` and run `./deploy.sh config`. A tile looks like:

```json
{ "label": "Snip", "icon": "✂️", "key": "win+shift+s",
  "mac": { "key": "cmd+shift+4" }, "linux": { "key": "printscreen" } }
```

The full action reference (keys, media, typed text, sequences, wheel, page/dial
switching, per-OS overrides) is in [device/README.md](device/README.md#configjson).

## Recovery

- If the service crashes, the USB device falls back to plain ADB, so you can
  always reach the Car Thing again.
- **Safe mode:** hold the back button while the Car Thing boots. The pad then
  leaves USB as ADB only.
- **Stock UI came back by itself?** The bootloader switched system slots. Run
  `./deploy.sh status`, then `./deploy.sh install`.
- `./deploy.sh uninstall` removes the boot hook and restores the stock UI.

## Known limitations

- **Mic toggle:** Linux gets a real system-wide mic-mute key. Windows has none,
  so it sends Win+Alt+K, which works in Teams and other apps that support Windows
  11's call-mute shortcut. On macOS it sends Teams' mute (⌘⇧M).
- **Linux overrides assume GNOME** (Fedora Workstation, Ubuntu). On KDE a few
  desktop shortcuts need adjusting.
- **macOS F11:** the "Step into" preset sends F11, which macOS uses for Show
  Desktop unless you turn that shortcut off.
- **No clock and no "now playing":** the pad only talks to the computer as a USB
  keyboard, and computers never send time or track info to a keyboard.
