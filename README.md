# streamthing

Turns a Spotify Car Thing into a USB macro pad and media controller, a small
Stream Deck. It needs **no app on the computer**. The computer sees a standard
USB keyboard, media keys and mouse wheel, so it works on Windows, macOS and Linux
as soon as you plug it in.

- **Touchscreen:** pages of large tiles (Media, Desktop, Shell, and a calls
  page: Discord or Meetings), each sending a shortcut, a media key, typed text or a sequence of steps,
  and live pages from the computer agent (Calendar, Status, Code).
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
  direction, calls page (Discord or Meetings), themes, device info, and Reconnect / Restart / Reboot.
- **Themes:** Dark, Black, Light, and the editor color schemes Nord, Dracula,
  Catppuccin, Gruvbox, Tokyo Night and Solarized, each with its own accent or
  one of six accent colors or any hue from a slider. Icons come in three styles (solid, outline, emoji) and three colors (neutral,
  accent, or a color per tile category).
- **Computer agent (optional):** a small background agent on the computer adds a
  **now playing** view to the Media page (cover, title, artist, album, app, live
  progress), a **Calendar** page with your Google Calendar for the coming days and
  Join buttons for meetings, a **Status** page with CPU, memory, GPU and network,
  a **clock**, and a **lock screen** with media controls (see
  [Computer agent](#computer-agent)).
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

Shortcuts differ per OS, so tiles can carry `"windows"`, `"mac"` and `"linux"`
overrides. The defaults use standard shortcuts: Mission Control and Spotlight on
macOS, GNOME's on Linux.

Over USB, Windows identifies itself, but macOS and Linux look the same. So:

- **Windows:** detected automatically.
- **Linux:** used automatically when the host isn't Windows (`"other_os": "linux"`
  in `config.json`).
- **macOS:** choose **macOS** under Settings → General → Host OS, or by tapping
  the status pill. On a Mac, shortcut hints are shown as ⌃⇧⌥⌘.

The choice is saved on the Car Thing. If you move between a Mac and a Linux
machine, switch it in Settings.

## Calls page

The last tab is either **Discord** (mute, deafen, answer/decline, quick switcher,
back to voice, inbox, mark read) or **Meetings** (Teams, Zoom and Meet mic/camera).
Pick it under Settings → General → Pages → Calls page. **Auto** shows Discord on
Windows and Meetings on macOS and Linux. Discord uses its default shortcuts;
mute and deafen work even when Discord isn't focused.

## Keyboard layout

The pad sends key *positions*, so typed text (the Shell tiles, for example) has
to match the layout the computer uses. Set it under Settings → General → Keyboard
layout:

- **Latin American:** matches Windows "Latin American" and Linux `latam`.
- **US:** use this on a Mac. macOS's own "Latin American" layout puts symbols in
  different places.

Shortcuts made of letters, digits and named keys work with any layout.

## Computer agent

The pad itself is just a USB keyboard, and computers never send track info, the
time or their lock state to a keyboard. An optional agent on the computer sends
them to the Car Thing **over the USB cable** (`adb forward`; no network):

- **Now playing:** the Media page shows the cover, title, artist, album, the app it plays in,
playing/paused and a live progress bar, with the media controls in a row below.
  When nothing is playing, or the agent isn't running, it's the normal tile page.
- **Calendar:** the Calendar page shows today, tomorrow and the rest of the week
  from your calendars (see [Adding your calendar](#adding-your-calendar)): repeating
  events, moved or cancelled occurrences and time zones included, meetings you
  declined left out. The next event is highlighted with a countdown. Events with a
  Google Meet, Zoom or Teams link get a Join button that opens the link on the computer.
  Tap an event for its details: date, time and length, place, calendar and notes.
- **Status:** the Status page shows CPU, memory, GPU and network speed, each with
  a graph of the last two minutes. The GPU needs an NVIDIA card (`nvidia-smi`), or
  an AMD one on Linux; CPU temperature shows on Linux.
- **Kill switch:** below the stats, the Status page shows the app that has focus on
  the computer, next to a covered switch like an aircraft's. Tap the cover to lift
  it; once "KILL" lights up (about a second later), press it to force-quit that app.
  The cover closes again after 6 seconds. Only the app shown is killed, and only if
  it still has focus. The system and desktop shell (Explorer, the window manager,
  Finder, the Dock, …) and the agent itself are protected. Finding the focused app:
  - Windows: the foreground window; apps running as administrator can't be killed
    unless the agent runs as administrator too.
  - macOS: the frontmost app (`lsappinfo`, no permission prompts).
  - Linux: X11 (`xprop`), Hyprland, Sway, KDE Wayland with `kdotool`, and GNOME
    Wayland with the [Window Calls](https://extensions.gnome.org/extension/4724/window-calls/)
    extension, since GNOME doesn't share the focused window otherwise.
- **Code (GitHub):** the latest run of each workflow in one repository (up to 5:
  running, queued, passed, failed or cancelled, with branch, commit and time) and
  your open pull requests on any repository, with their checks, review state and
  draft flag. Tap a row to open it in the browser on the computer. It refreshes every
  minute, or every 15 seconds while a workflow runs (see [Adding GitHub](#adding-github)).
- **Volume and audio devices:** when the volume or mute changes (the dial, the Mute
  tile, your keyboard), a volume bar shows at the top for a moment. The Media page
  header shows the current output; tap it to pick the output and input devices.
  - Windows: Core Audio, the same switch the Sound settings use (all roles, so calls
    move too).
  - Linux: `pactl` (PipeWire or PulseAudio).
  - macOS: the volume always; switching devices needs `brew install switchaudio-osx`.
- **Clock:** the agent sets the Car Thing's clock and time zone (DST included).
  The time shows in the bottom bar, large with the date at the top of the Media
  page and on the standby screen; 24h/12h is under Settings → General → Clock.
- **Lock screen:** while the computer is locked, the Car Thing shows the clock,
  "🔒 Computer locked", the cover and what's playing, and media buttons. Tiles,
  presets and dial modes that press keys are blocked while locked, so nothing gets
  typed into the password box; media keys, volume and the wheel still work.

Install it once per computer (and once per OS on a dual-boot PC). It starts at
login, survives unplugging and sleep, and needs `adb` (see [Requirements](#requirements)):

| OS | Reads from | Install |
|---|---|---|
| Windows | The system media session (Spotify, browsers, Media Player, …) | `powershell -ExecutionPolicy Bypass -File host\windows\install.ps1` |
| Linux | MPRIS through `playerctl` (Spotify, browsers, VLC, …) | `host/unix/install.sh` |
| macOS | `nowplaying-cli`, or Spotify and Music through AppleScript | `host/unix/install.sh` |

- **Windows:** a Task Scheduler task at logon starts a tiny windowless launcher
  in `%USERPROFILE%\.streamthing`. Log: `%USERPROFILE%\.streamthing\nowplaying.log`.
- **Linux:** a systemd user service (`streamthing-nowplaying`). Needs `playerctl`
  (`sudo dnf install playerctl`). Log: `journalctl --user -u streamthing-nowplaying`.
- **macOS:** a LaunchAgent. `brew install nowplaying-cli` is recommended; recent
  macOS versions restrict it, in which case the agent asks Spotify or Music
  directly (macOS asks once to allow that).

Uninstall with `install.ps1 -Uninstall` or `install.sh --uninstall`. To check what
the agent sees without installing: `nowplaying.ps1 -Once` or `nowplaying.py --once`.

### Adding your calendar

The agent reads calendars from their private iCal address, so it needs no sign-in:

1. In Google Calendar on the web, open Settings, pick your calendar under
   "Settings for my calendars", and copy **Secret address in iCal format** from
   "Integrate calendar".
2. Put it on its own line in `calendars.txt` (create the file):
   - Windows: `%USERPROFILE%\.streamthing\calendars.txt`
   - Linux: `~/.config/streamthing/calendars.txt`
   - macOS: `~/Library/Application Support/streamthing/calendars.txt`

   Add one line per calendar. Any iCal address works (Outlook's published calendars
   too); lines starting with `#` are ignored.
3. The agent downloads the calendars every 5 minutes, so they show up within 5
   minutes; no restart needed.

The secret address lets anyone who has it read that calendar, so keep the file to
yourself; if it leaks, reset it on the same Google Calendar settings page. Work
accounts may have it turned off by an administrator.

### Adding GitHub

The agent asks GitHub through the [GitHub CLI](https://cli.github.com), which keeps
your sign-in; no token is stored by streamthing.

1. Install it and sign in once:
   - Windows: `winget install GitHub.cli`
   - Fedora: `sudo dnf install gh`
   - macOS: `brew install gh`

   Then run `gh auth login`.
2. Put the repository for the Code page in `github.txt`, next to `calendars.txt`:
   its name if it is one of yours (`my-repo`), or `owner/name`. Lines starting with
   `#` are ignored.

The file stays on your computer, so the repository's name is never in this repo.
Your open pull requests come from any repository; the Actions card shows that one.

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

- **Linux overrides assume GNOME** (Fedora Workstation, Ubuntu). On KDE a few
  desktop shortcuts need adjusting.
- **macOS F11:** the "Step into" preset sends F11, which macOS uses for Show
  Desktop unless you turn that shortcut off.
- **Now playing, the calendar, stats, the clock and the lock screen need the agent**
  on each computer; without it, there's no clock, the Media page is the plain tile
  grid, and Calendar and Status say the agent is missing. The macOS and Linux agents
  are untested on real machines so far.
- **No Windows CPU temperature:** Windows only gives it to programs running as
  administrator with a hardware driver, so the Status page leaves it out there.
