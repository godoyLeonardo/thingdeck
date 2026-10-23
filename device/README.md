# device/

Everything in this folder runs on the Car Thing from `/var/macropad`, which is on
the writable data partition. `../deploy.sh` copies it there.

| File | What it does |
|---|---|
| `macropad.py` | The service: USB HID over FunctionFS, dial and button input, actions, settings, and the local web UI server |
| `ics.py` | iCalendar reader for the Calendar page (repeats, exceptions, time zones), plain Python 3.7 |
| `gadget.sh` | Rebuilds the USB gadget (`prepare`, `bind-hid`, `adb-only`) |
| `start.sh` | Supervisor entry point. Always puts USB back to ADB-only when the service exits |
| `hook.sh` | Installs/removes the boot hook on both A/B system slots (`install`, `uninstall`, `status`) |
| `macropad.conf` | Supervisor program, installed to `/etc/supervisor.d/` |
| `config.json` | Pages, tiles, presets, dial modes, hardware buttons, layout, OS defaults |
| `web/index.html` | Touch UI shown in the kiosk browser |
| `web/shim.html` | Bind-mounted over the stock UI's `index.html`; redirects to the service once it is up |
| `tools/cdp.py` | DevTools helper for the kiosk browser: `screenshot`, `eval`, `reload` |
| `tools/check.py` | Smoke test that `deploy.sh check` runs here: service, agent, each feed, each page |

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
the backlight drops to its minimum (`BRIGHTNESS_MIN`, and auto brightness pauses);
the brightness setting is applied again when the host is back.

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
`IOConsoleLocked` in `ioreg` (macOS; older versions listed `CGSSessionScreenIsLocked`).

### Now playing

A host agent (`../host/`) posts the current track to `POST /api/nowplaying` through
`adb forward tcp:28765 tcp:8765`, so it arrives as a local request. `NowPlaying`
keeps the metadata and the cover separately: the cover comes base64 with an
`art_key` (a hash of title, artist, album and app) only when the key changes, or
when the reply asks for it (`need_art`, e.g. after a service restart). Position
updates every 2 s stay small. The UI gets `nowplaying` events over SSE, loads the
cover from `/api/nowplaying/art?k=…`, and moves the progress bar between updates.
With no update for 15 s the data is dropped (computer off, agent stopped).

A page with `"header": "clock"` (Media, Lights, Calendar, Status, Code and Plex, by default)
shows a big clock and the date in place of the preset labels and dial chip, as long
as the agent has set the time; the presets and dial still work. A page with
`"nowplaying": true` (Media) is the media card: cover, details and progress on top
(with nothing playing: a large clock and the date, while the top bar hides its own),
and its tiles as a compact row of round controls below.

### Touch UI

The bottom bar holds one tab per shown page plus ⚙ Settings. Up to seven tabs share
it evenly (seven with smaller text). With more, each tab is a quarter of the bar, so
four show at a time; the bar scrolls sideways, snaps to whole tabs, and keeps the
current page's tab in view.
The arrow button at its end then points to the other tabs (› scrolls on, ‹ goes back
to the start), and a drag that starts on the bar never switches pages. Details and
the audio picker open as a card over the page; tapping outside it closes it.
A live page's tab can show a dot after its name (`TAB_DOTS`, by widget): Plex while
someone streams, and NAS in the accent color while TrueNAS answers or red when it fails.
With updates waiting (apps, and a newer TrueNAS), the NAS tab shows their number in a
badge, in the warning color.

In a page's clock header, every page but the media card shows the song playing on
the right (title, then artist, cut off from the end when space runs out); tapping
it goes to the first page with `"nowplaying": true`.

### Motion and CPU

The kiosk browser is Chromium 69. It composites on the GPU (Mali-G31) but paints on the
CPU, and every frame it makes costs a fixed share of a core. A CSS animation or transition
that never stops makes 60 frames a second: on the Car Thing, the equalizer bars of the
song playing kept 60-66% of a core busy on every live page, and the Media page about
140%. So no CSS animation runs without stop:

- `animate()` moves the equalizer bars at 24 frames a second (`MOTION_FPS`), each on its
  own layer (`will-change: transform`). They cost about 55% of a core while they show,
  and nothing when no song plays. Measured: 8 frames about 20%, 24 frames without their
  own layer about 68%; a CSS animation costs the same with or without its own layer.
  The spinners (waiting cards, GitHub runs and checks) turn at the same rate, one turn
  a second.
- The dot of a meeting in progress and the reminder card's border blink once a second.
- The progress bars move once a second, with no transition (a song's bar moves about
  2 px a second, so a transition adds nothing).

Short transitions that run once (a tap, a page change, the volume bar) are fine.

### Calendar and system stats

A page with `"widgets"` shows live panels instead of tiles: `["agenda"]` (Calendar)
lays the coming days out as Today / Tomorrow / Next two weeks columns,
`["monitor"]` (Status) shows CPU, memory, GPU and network cards, and
`["agenda", "monitor"]` puts a list and a 2×2 grid side by side.

**Stats:** the agent posts `POST /api/stats` every 2 s. `HostStats` keeps the last
60 samples for the graphs and sends `stats` events; with no update for 10 s it
clears them. The reply carries a meeting link to open (`open`) and whether the
service needs the calendars again (`need_calendar`, e.g. after a restart), so the
agent needs no second request.

**Calendars:** the agent downloads the addresses in `calendars.txt` every 5 minutes (every
minute while the Calendar page is on screen or `calendar_soon` is true, see [What updates
when](#what-updates-when)) and
posts the iCal text as it is to `POST /api/calendar` when it changes (`?none=1`: no
`calendars.txt`; `?error=`: a download failed). `ics.py` reads it here, so the logic
exists once for every OS: VTIMEZONE observances (DST included), RRULE
(DAILY/WEEKLY/MONTHLY/YEARLY with COUNT, UNTIL, INTERVAL, BYDAY, BYMONTHDAY, BYMONTH,
BYSETPOS), EXDATE, RDATE, RECURRENCE-ID (moved or cancelled occurrences), and events the
calendar's owner declined (a Google feed's name is the owner's address). One-off events
that ended before today are skipped while reading: a 4 MB feed takes about 2 s on the
Car Thing. `Agenda` lists the events from local midnight to 28 days later
(`AGENDA_WEEKS`, the most the page shows), recomputes every 5 minutes and when the UTC
offset changes, and sends `agenda` events. All-day and floating times use the computer's
UTC offset. An `agenda` event also has `calendars`: a name for each calendar, by the
events' `cal` index (its `X-WR-CALNAME`, "Calendar 2" without one, and the number added
to a name used twice). With two names or more, the page shows tabs above the agenda: All
and each name. The tab is the setting `calendar` (a name, or `null` for All) in
`state.json`. A name that is not in `calendars` shows All. On the left of the bar, − and
+ set how many weeks the page shows, 1 to 4 (2 when none is saved), for each tab: the
setting `calendar_weeks`, `{"<name>" or "" for All: weeks}`. The page filters the 4
weeks the device sends. Each calendar has a color by its index (`CAL_COLORS`, the
theme's own colors): a dot on its tab, and before the title of its events in All. The
tabs filter only the page: reminders come from every calendar.

**Join:** the UI posts `POST /api/calendar/open` with an event id; only events with
a Meet, Zoom or Teams link have one. The link goes out once, in the reply to the
agent's next stats update (within 2 s), and the agent opens it only if it is an
`https://` link of one of those services.

**Event details:** the agenda carries what the list shows; tapping an event fetches
its notes (descriptions, HTML turned into plain text, up to 2000 characters) and
calendar name from `GET /api/calendar/event?id=…`.

**Reminder:** the UI checks the agenda every second and shows a card for the first
timed event (all-day ones aside) from 5 minutes before its start to 2 minutes after
(not over standby or the lock screen). An event with a meeting link gets Join, which
opens the link as above; any other gets Details, which opens its details card. Either
button and Dismiss hide it for that event, remembered in the browser's `localStorage`
(`thingdeck.dismissed`, kept for a day).

### Kill switch

A page with `"kill": true` (Status) adds a strip under its widgets with the focused
app, which the agent sends in each stats update as `focus`: `{pid, name, app, title,
protected}`, or `{error}` when it can't tell (e.g. GNOME Wayland without the Window
Calls extension). The switch is a cover over a button: the first tap lifts the cover
and locks in that app's pid; the button answers only 900 ms later, so a double tap
can't do both; the cover drops after 6 s or when focus moves. Pressing it posts
`POST /api/kill {pid}`. The service refuses unless the pid is still the focused app
and not protected, then hands `{pid, name, app}` to the agent in its next stats reply.
The agent checks the process still has that pid and name and isn't protected (the
OS, the desktop shell, the agent itself), kills it (`Stop-Process -Force` on
Windows, SIGKILL elsewhere), and reports `kill_result` in the next update, which
shows as a toast.

### Themes

`web/index.html` defines each theme as CSS variables under
`:root[data-theme=…]`. Settings saves `theme` and `accent` in `state.json`, and
the page sets `data-theme` and the `--acc`/`--acc-soft` accent variables. Accent
`theme` uses the color scheme's own accent; the fixed accents have a lighter and
a darker shade for dark and light themes. The hue slider stores `accent` as a
number (0–359), drawn light on dark themes and dark on light ones.

### Brightness

`/sys/class/backlight/aml-bl/brightness` is **inverted**: 0–255, and higher means
dimmer, so a level is written as `260 - level`. The stock `sp-als-backlight` service
has no offset, so it stays stopped while the pad runs. When the pad service stops in
auto mode (not in standby), it starts the stock service again, so the screen still
follows the light; the next start stops it again. In auto mode, `Pad.follow_sensor` reads
the TMD2772 light sensor (`in_intensity0_raw`) every 0.5 s. It uses the stock curve,
fitted to that service's log: level 25 in the dark, `63.3 + 19.3 ln(raw - 12.2)` above a
reading of 12, and 210 at most. It multiplies the level by 2 for each stop of the offset
(`brightness_stops`, -3 to +3 in half stops; Settings → General). Then it fades a
fifth of the way to that level on each reading, as the stock service did.

At night the level drops by `night_stops` more (0, off, to -3), in auto and in manual
mode: from `night_from` to `night_to`, minutes of the day by the computer's clock
(22:00 to 07:00 by default; the hours can cross midnight). The same loop checks it every
0.5 s, and the state's `night` says when it is on. Before the agent has set the clock,
it is never night.

### GitHub

The widget `"github"` (the Code page) shows what the agent reads with the `gh` CLI,
for the repository in `github.txt` on the computer: the newest run of each workflow
in the last 100 runs (20 at most) and the user's open pull requests (one GraphQL
query, any repository, with the check rollup and review decision). Both lists scroll,
and a redraw keeps their scroll position. The agent posts it to `POST /api/github`
every minute, or every 15 s while a run is queued or running; `{"none": true}` means
there is no `github.txt`. `GitHubStatus` keeps each row's link and opens a row by its
id; the UI gets the link only to show it. A tap opens a card with the details and the link;
its **Open on computer** button posts `POST /api/github/open {id}`, which sends the link to the agent
through the same `LinkOpener` as meeting links (the agent opens `https://github.com/`
links and meeting links only).

When a card opens, it asks for more: the page posts `POST /api/github/ask {id}`, and the
next command poll (`GET /api/commands`) gives the agent `github_details`, `{row, seq,
kind, repo, run_id or number}`. The agent reads the run
(`repos/{repo}/actions/runs/{id}`) and its jobs (`…/jobs`), or the pull request (`gh pr
view`: branches, size, merge state, reviews and the check rollup) with `gh`, in a thread
on Linux and macOS (the Windows loop waits for it), and posts `POST
/api/github/details`. The service passes that on as the event `github_details`. A job
comes with its step counts, the step that runs and the step that failed, not with every
step. While the run or a check goes on, the page asks again every 5 s; with no answer in
15 s, the card says so.

The page remembers the runs it has seen, by their links. A new run, or a run that runs
again, while another page is on screen and no other card, reminder or Settings is open,
shows a small card with Go to Code (`checkNewRuns`); it closes by itself after 20 s. The
agent reads GitHub every minute, so the card can come up to a minute late. Before GitHub
first answers, after an error with no runs, and for another repository, the page
remembers nothing, so the runs already there don't count.

### What updates when

The command poll's reply carries `visible`, the widgets of the page on screen
(`Pad.visible_widgets()`; none on the standby or lock screen). The agent reads those
often and the rest slowly:
Hue 2 s / 30 s, Plex 3 s / 30 s, TrueNAS 10 s / 60 s, GitHub 15 s while a workflow runs / 1 min, and
the GPU part of the stats 2 s / 30 s (the rest of the stats, the song, audio, the host
heartbeat keep their pace). A widget that comes on screen is read at once (the calendars
at most twice a minute). The calendars are read every minute while the Calendar page is on
screen or the poll's `calendar_soon` is true (`Agenda.soon()`: a timed event starts within
15 minutes, so a late change still reaches the reminder), else every 5 minutes. `PLEX_TTL`
is 75 s so slow Plex updates don't expire.

When the forward comes back (the agent's start, a replug, the Car Thing restarted), the
agent forgets what it last sent and sends everything at once: the song and cover, the
heartbeat, the calendars, audio, Hue, Plex, TrueNAS and GitHub.

### Philips Hue

The Car Thing has no network, so the agent talks to the Hue Bridge (local API v2,
`https://<bridge>/clip/v2/…` with the `hue-application-key` header) and posts the
result to `POST /api/hue`: `{state: "ok", rooms: [...]}` with each room and zone, its
`grouped_light` id, on/brightness, its lights (on, brightness, CIE `xy` or `mirek`)
and its scenes; or `{state: "none" | "pairing" | "error"}`. Structure (rooms, zones,
devices, scenes) is read every 30 s, levels every 2 s and right after a command.
The bridge's certificate comes from Philips' own authority, so the agent accepts it
for the bridge's address only.

Commands go the other way through a queue: the UI posts `POST /api/hue/command`
(`{op: "group" | "light", id, on?, brightness?, mirek?}`, `{op: "scene", id}`,
`{op: "all_off"}`, `{op: "pair"}`), and the agent collects them with
`GET /api/commands` 4 times a second (once elsewhere). `HostHue` merges commands for
the same light or group, so a drag or a fast dial turn sends one brightness, and
updates its state right away (the next state from the agent corrects it). The
widget `"lights"` draws a card per room (tinted with the average color of its lit
lights, from `xy` or `mirek`); `POST /api/hue/select` makes a room the dial's
target, and the action `{"hue": "up" | "down" | "toggle" | "all_off"}` (the
**Lights** dial mode) changes it by 8 % per detent and sends a `hue_dial` event for
the on-screen bar.

Pairing: `{op: "pair"}` makes the agent find the bridge (Philips' discovery service),
then ask `POST /api` for a key every 2 s for 30 s while you press the bridge's
button; the address and key go to `hue.json` on the computer.

### Plex

The Car Thing has no network, so the agent reads the Plex Media Server whose address
is the first line of `plex.txt` on the computer. A second line holds a Plex token,
which the agent sends as the `X-Plex-Token` header. The agent asks for JSON
(`Accept: application/json`). The Windows agent reads it with `JavaScriptSerializer`,
because Plex items can have keys that differ only in case (`guid` and `Guid`).
`ConvertFrom-Json` in Windows PowerShell refuses those keys.

Every 3 s while the Plex page is on screen (every 30 s otherwise, see [What updates
when](#what-updates-when)) the agent reads `/`, `/status/sessions`, `/activities`
(library scans and scheduled tasks while they run, shown as Jobs under the streams), and
the server's own history of load and bandwidth (`/statistics/resources` and
`/statistics/bandwidth`, Plex Pass only). Then it posts the result to `POST /api/plex`.
Every 5 minutes it counts the items of each library and reads `/updater/status`. A count
is one request with `X-Plex-Container-Size=0`, which gives `totalSize` and no items.
When the server does not answer, the agent tries again after 30 s, so the other pages do
not wait.

Each stream also has `details` for its card on the page: the library, year and rating;
the player (product, platform, device, address, relayed, secure); the media
(container, codecs, resolution, frame rate, channels, bitrate); the streams picked, with
Plex's decision for each; and for a transcode, what changes (from and to which codecs),
its speed and progress, throttling, and hardware decoding and encoding. They come from
the same `/status/sessions` reply, so they cost no request.

The page remembers the stream ids it has seen. A new one, while another page is on screen
and no other card, reminder or Settings is open, shows a small card with Go to Plex
(`checkNewStreams`); it closes by itself after 20 s. Before Plex first answers, and after
it stops answering, the page remembers nothing, so streams already on don't count.

The bandwidth history has one row per device and sample, at uneven times. The agent
adds up the rows per 6 s, LAN and WAN apart, and sends bytes per second.

`HostPlex` keeps the last update and clears it 30 s after the agent stops. Posters
work like the now-playing cover. Each stream has an `art` key, a hash of its poster
path. The reply lists the keys that the device does not have (`need_art`). The agent
sends those posters in its next update, as base64 from Plex's photo transcoder.
`GET /api/plex/art?k=…` serves them with `max-age`, because a key always names the
same image.

The widget `"plex"` redraws the streams card and the side cards separately, and it
changes the progress bars in place.

### TrueNAS

The agent reads the TrueNAS server in `truenas.txt` (address, then an API key) through
its JSON-RPC 2.0 API over a WebSocket, `wss://<address>/api/current` (TrueNAS 25.04+;
the REST API is deprecated there). Each read opens one connection, calls
`auth.login_with_api_key`, then `system.info`, `pool.query` (only the fields it shows,
through `select`), `disk.temperatures`, `alert.list`, `app.query`, `core.get_jobs`
(the `RUNNING` jobs; without `select`, because TrueNAS fails on it for jobs) and
`update.status` (TrueNAS 25.10 and later: `system.update` is the newer version, if any), and the
scheduled tasks (`pool.scrub.query`, `pool.snapshottask.query`, `replication.query`,
`cloudsync.query`, `rsynctask.query`, `cronjob.query`), and closes it. Replies are
matched by `id`, so notifications in between are skipped. A failing call other than
`system.info` only leaves its part empty, and the agent logs it once, until it works again.

It is always TLS: TrueNAS revokes an API key that arrives over plain HTTP. The
certificate is usually self-signed, so the agents accept it for that address only (the
same callback as the Hue bridge on Windows; `CERT_NONE` on that one socket in Python).
Windows uses .NET's `ClientWebSocket`; Python has no WebSocket client, so
`nowplaying.py` has a small one (masked text frames, ping/pong, fragments).

Every 10 s while the NAS page is on screen, 60 s otherwise and after an error, the
agent posts `POST /api/truenas` with `{state, system, pools, disks, alerts, apps, jobs}`.
A job is `{method, target, percent, text, started}`. The page names it from the method
(`pool.scrub.scrub` is "Scrub" plus the pool), so the names exist once for both agents.
`tasks` holds the enabled tasks with a schedule, `{kind, name, schedule, threshold}`, and
each pool has `scrubbed`, the end of its last scrub. `HostTrueNAS` works out the next run
(`cron_next`: cron fields, `begin`/`end`, the computer's UTC offset) and publishes the
first one as `next`, `{kind, name, at}`. A scrub skips runs until its last scrub ended
more than `threshold` - 1 days before, as TrueNAS does.
It sends only changes, plus one at least every 30 s. Alerts leave out dismissed and
INFO ones. A call waits at most 5 s, because the Windows agent's main loop waits on it.
`HostTrueNAS` keeps the last update and clears it after 150 s without one. The widget
`"truenas"` shows pools as tanks (80% yellow, 90% red), disk temperatures (45/55 °C,
NVMe 60/70 °C), alerts, apps and running jobs.

### Weather

The agent reads `weather.txt` (a place, then optionally `fahrenheit`) and asks
Open-Meteo every 15 minutes (5 after an error) for the current `temperature_2m`,
`weather_code` and `is_day`. A place name is looked up once with Open-Meteo's geocoder
(a country after a comma picks among the matches). It posts `POST /api/weather` with
`{state, place, temp, code, day, unit}` when that changes, and at least every 30
minutes. `HostWeather` keeps it for 2 hours without an update, so it still shows on the
standby screen for a while after the computer sleeps. The UI puts it in every
`.bigweather` (under the large clocks of the idle Media card, the lock and the standby
screens: an icon and words for the WMO weather code, and the temperature) and in
`#topweather`, the top bar's icon and temperature, shown on the Media page while
something plays (`body.media-page:not(.media-idle)`).

### Tab order

`state.json` keeps `page_order`, a list of page keys (a page's `name`), and
`page_hidden`, the keys of hidden pages. `Pad.ordered_pages()`
(and `orderedEntries()` in the UI) sorts `config.json`'s pages by the order and
leaves out hidden ones (at least one always stays); pages the order doesn't name keep
their config order after the named ones, so new pages appear at the end. Page
indexes, the tabs, swipes and `{"page": "next"}` (the back button, wrapping around)
all work on the shown pages. Changing either setting keeps the current page. When
`config.json` loads, keys of pages it no longer has are dropped from `page_order`,
`page_hidden` and `page_shown`.

**Pages that aren't set up** hide on their own: `Pad.refresh_hidden()` (every 2 s) hides
a page whose widgets all report `none` (no `calendars.txt`, `github.txt`, `plex.txt` or
`truenas.txt`, Hue not paired). `unknown`, before the agent first reports, doesn't count.
Turning such a page on in Settings → Tabs adds it to `page_shown`, which wins over
this; turning any page off adds it to `page_hidden`, which wins over both. The state
carries `hidden` (the effective set) and `unconfigured` for the UI.

### Volume and audio devices

The agent checks the default output's volume and mute, and the default output and
input, several times a second (4 on Windows, 1 elsewhere), and posts `POST /api/audio`
when they change and at least every 2 s; device lists are read every 3 s and sent only
when they change (the reply asks for them with `need_lists` after a restart).
`HostAudio` sends `audio` events; the UI shows its volume bar when the volume or
mute changes, and right away when the volume dial turns. A page with `"audio": true`
(Media) shows the output in its clock header; tapping it opens the device picker,
which posts `POST /api/audio/set {id}`. The pick goes to the agent as `set: {id,
kind}` in the reply to its next audio update, so switching takes under a second.

### App volume (Windows)

The Windows agent reads the default output's audio sessions (`IAudioSessionManager2`, one
`ISimpleAudioVolume` each, as the Volume Mixer does) every second and groups them by
process name: an app's volume is its loudest session's, it counts as muted when all are,
and as playing when one is active. Its name is the program's file description ("Google
Chrome"), else the process name; the system sounds session is "System sounds". It posts
`POST /api/mixer {apps: [{key, name, volume, muted, active}]}` when that changes and at
least every 5 s; the reply's `need_icons` asks for each app's icon once, a 48 px PNG of the
program's icon sent in the next update (`icons: {key: base64}`), served at
`GET /api/mixer/icon?k=…&v=…` (`v` is a CRC of the PNG, so a new icon loads).

`HostMixer` keeps the list (15 s without an update: gone) and a command queue merged per
app, which the agent collects with the command poll (`mixer: [{key, volume?, mute?}]`) and
applies to every session of that process; the key `*` is All, the output's own volume
(`IAudioEndpointVolume`). A value set on the pad wins over the agent's readings for 2 s, so
the bar doesn't jump back while you drag or turn. The action `{"mixer": ...}`: `"next"`
moves the dial's target from All (`selected: null`) to each app, in the list's order
(playing first), and back to All; `"up"` / `"down"` change the target's volume by 4;
`"toggle"` mutes the target. On All, and without the agent's apps (not Windows, no agent),
they are the volume keys and mute, so the default config's Volume mode uses them on turn
and press everywhere. The knob, and turning an app, make the UI open the mixer full screen
over the page on screen (`mixer_dial` events, with `open`) and close it again 6 s after
the dial's last use; turning All only shows the usual volume bar.

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
| `presets` | Default 4 top-button actions (first page `{"page": 0}`, `"prev"`, `"next"`, last page `{"page": -1}`); 2 and 3 have a `hold` for the previous and next track |
| `pages` | `[{"name", "presets"?, "grid"?, "nowplaying"?, "header"?, "widgets"?, "kill"?, "audio"?, "tiles": [...]}]` |

A preset can also have `hold`, an action with its own `label`, for a press of 0.45 s or
longer (`HOLD_SECONDS`). A preset with `hold` fires its own action on release; one
without it fires on press.

Tiles and presets are actions plus `label`, optional `icon` (an emoji) or `symbol`
(a built-in icon drawn in the current icon style, or as an emoji in the emoji style),
`hint` (small text; generated if missing) and `color`. In the media card's control
row, `"wide": true` makes a tile double width. A tile's own `color` also colors its
symbol whatever the icon color setting (red for a hang-up tile, say).

Symbols: `play pause prev next stop mute note` (media), `windows desktop snip
clipboard folder smile activity lock search`, `command terminal files target pencil
format sidebar`, `branch download diff list flask launch repeat interrupt`, `mic
camera hand phone hangup headphones speaker inbox check`, and `x clock slash alert
pr` (also used for GitHub statuses), and `bulb power`.

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
| Hue | `{"hue": "up"}` / `"down"` / `"toggle"` / `"all_off"` (the selected room; needs the agent paired with a Hue Bridge) |
| Volume, per app | `{"mixer": "next"}` / `"up"` / `"down"` / `"toggle"` (the dial's target, All or one app, on Windows; the volume keys and mute elsewhere) |

**Per-OS overrides:** any action can carry `windows`, `mac` and/or `linux`
objects. When the host matches, the override replaces the action, keeping
`label`/`icon`/`symbol`/`color` unless it sets them. This works inside sequences too:

```json
{ "label": "Task view", "key": "win+tab",
  "mac": { "label": "Mission Control", "key": "ctrl+up" },
  "linux": { "label": "Overview", "key": "win" } }
```


## HTTP API

Served on `127.0.0.1:8765`, reachable only from the Car Thing itself (the kiosk
page, or `adb shell`).

| Method | Path | Body | Purpose |
|---|---|---|---|
| GET | `/api/ping` | | Health check (used by `shim.html`) |
| GET | `/api/state` | | Config, state, presets, now playing, stats, agenda, audio, github, hue, mixer, plex, truenas, weather |
| GET | `/api/events` | | SSE stream: `state`, `config`, `fired`, `turn`, `hw`, `toast`, `nowplaying`, `stats`, `agenda`, `audio`, `github`, `github_details`, `hue`, `hue_dial`, `mixer`, `mixer_dial`, `plex`, `truenas`, `weather`, `view` |
| GET | `/api/info` | | Uptime, temperature, memory, load, brightness |
| POST | `/api/tap` | `{"page", "index"}` | Run a tile |
| POST | `/api/preset` | `{"index"}` | Run a preset |
| POST | `/api/page`, `/api/dial`, `/api/os` | `{"page"}` / `{"dial"}` / `{"os"}` | Switch page / dial mode / host OS |
| POST | `/api/view` | `{"settings"}` | The page shows Settings or not. From Settings, `"next"` goes to the first page and `"prev"` to the last; any page move closes Settings (event `view`) |
| POST | `/api/settings` | `{"theme": "nord", …}` | Change settings (saved in `state.json`) |
| POST | `/api/host` | `{"time_ms", "utc_offset_min", "locked"}` | Heartbeat from the host agent: sets the clock, lock state |
| POST | `/api/nowplaying` | `{"title", "artist", "album", "album_artist", "track_number", "app", "playing", "position_ms", "duration_ms", "shuffle", "repeat", "art_key", "art"?, "art_type"?}` | From the host agent; `{"title": null}` clears. Replies `{"need_art": bool}` |
| GET | `/api/nowplaying`, `/api/nowplaying/art` | | Current track metadata; its cover image |
| POST | `/api/stats` | `{"cpu", "cpu_temp"?, "mem_used_mb", "mem_total_mb", "gpu"?, "gpu_temp"?, "gpu_mem_used_mb"?, "gpu_mem_total_mb"?, "net_rx", "net_tx"}` | From the host agent every 2 s. Also `focus` and `kill_result`. Replies `{"open": link or null, "kill": target or null, "need_calendar": bool}` |
| POST | `/api/calendar` | iCal text (`?none=1`, `?error=…`) | From the host agent when its calendars change |
| POST | `/api/calendar/open` | `{"id"}` | Open an event's meeting link on the computer |
| GET | `/api/calendar/event?id=…` | | An event's notes and calendar name |
| POST | `/api/kill` | `{"pid"}` | Kill the focused app (it must still have focus); goes to the agent with its next stats reply |
| POST | `/api/audio` | `{"volume", "muted", "output", "input", "outputs"?, "inputs"?}` | From the host agent. Replies `{"set": {"id", "kind"} or null, "need_lists": bool}` |
| POST | `/api/audio/set` | `{"id"}` | Make an output or input the default on the computer |
| POST | `/api/github` | `{"repo", "runs", "prs", "error"?}` or `{"none": true}` | From the host agent |
| POST | `/api/github/open` | `{"id"}` | Open a run or pull request on the computer |
| POST | `/api/github/ask` | `{"id"}` | A card asks for its details (the agent fetches them) |
| POST | `/api/github/details` | `{"row", "seq", "kind", …}` | From the host agent: the details a card asked for |
| POST | `/api/hue` | `{"state", "rooms"?, "error"?}` | From the host agent |
| POST | `/api/hue/command` | `{"op", "id"?, "on"?, "brightness"?, "mirek"?}` | Queue a Hue command for the agent |
| POST | `/api/hue/select` | `{"id"}` | Make a room the target of the Lights dial mode |
| POST | `/api/mixer` | `{"apps": [...], "icons"?}` | From the Windows agent every second on a change. Replies `{"need_icons": [keys]}` |
| POST | `/api/mixer/command` | `{"key", "volume" \| "delta" \| "mute"}` | Set an app's volume or mute (`key` `*`: All); `mute` can be `"toggle"` |
| POST | `/api/mixer/select` | `{"key"}` | Make an app (or `*`, All) the target of the App volume dial mode |
| GET | `/api/mixer/icon?k=…` | | An app's icon |
| POST | `/api/plex` | `{"state", "server"?, "sessions"?, "load"?, "net"?, "libraries"?, "activities"?, "posters"?, "error"?}` | From the host agent every 3 s. Replies `{"need_art": [keys]}` |
| GET | `/api/plex/art?k=…` | | A stream's poster |
| POST | `/api/truenas` | `{"state", "system"?, "pools"?, "disks"?, "alerts"?, "apps"?, "jobs"?, "error"?}` | From the host agent every 10 s on screen, 60 s otherwise |
| POST | `/api/weather` | `{"state", "place"?, "temp"?, "code"?, "day"?, "unit"?, "error"?}` | From the host agent every 15 min |
| GET | `/api/commands` | | For the host agent: queued commands (`{"hue": [...], "mixer": [...], "github_refresh": bool, "github_details": ask or null}`, emptied on read), the panels on screen (`"visible"`) and `"calendar_soon"` |
| POST | `/api/github/refresh` | | Ask the agent to read GitHub now (pull down on the Code page) |
| POST | `/api/run` | any action | Run an action |
| POST | `/api/system` | `{"do": "reconnect" \| "restart" \| "reboot"}` | Reconnect USB (software replug) / restart service / reboot |

## Debugging

```bash
adb shell tail -f /var/log/macropad.log         # service log
adb shell supervisorctl status                  # all services
adb shell python3 /var/macropad/tools/cdp.py screenshot /tmp/s.png && adb pull /tmp/s.png
adb shell python3 /var/macropad/tools/cdp.py eval "JSON.stringify(st)"
adb shell curl -s http://127.0.0.1:8765/api/state
./deploy.sh check                               # the whole smoke test, screenshots in check-shots/
```

Restarting the service re-creates the USB device, which also drops ADB. Run it
detached (`deploy.sh` does this) or the restart gets cut off midway:

```bash
adb shell 'setsid sh -c "supervisorctl restart macropad" </dev/null >/dev/null 2>&1 &'
```
