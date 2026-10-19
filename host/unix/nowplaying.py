#!/usr/bin/env python3
"""streamthing agent for Linux and macOS.

Sends the Car Thing the computer's time (for its clock) and lock state, system stats
(CPU, memory, GPU, network), the calendars listed in calendars.txt, and what it is
playing, over USB through `adb forward`. It also opens meeting links the Car Thing
asks for. What is playing comes from:
  Linux  playerctl (any MPRIS player: Spotify, browsers, VLC, ...)
  macOS  nowplaying-cli; if that returns nothing (macOS 15.4+ restricts the API it
         uses), AppleScript for Spotify and Music

  python3 nowplaying.py          # run
  python3 nowplaying.py --once   # print one snapshot

install.sh sets it up to start at login. Environment: ADB (path to adb),
STREAMTHING_PORT (host side of the forward, default 28765), STREAMTHING_CALENDARS
(calendars.txt; default ~/.config/streamthing/ on Linux,
~/Library/Application Support/streamthing/ on macOS).
"""
import base64
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

PORT = int(os.environ.get('STREAMTHING_PORT', '28765'))   # the device listens on 8765
ADB = os.environ.get('ADB') or shutil.which('adb') or 'adb'
MAX_COVER = 3 * 1024 * 1024


def run(cmd, timeout=5):
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=timeout, universal_newlines=True)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ Linux

SEP = '\x1f'
PLAYERCTL_FIELDS = ['status', 'title', 'artist', 'album', 'xesam:albumArtist', 'xesam:trackNumber',
                    'mpris:length', 'position', 'mpris:artUrl', 'playerName']


def linux_snapshot():
    out = run(['playerctl', 'metadata', '--format', SEP.join('{{%s}}' % f for f in PLAYERCTL_FIELDS)])
    if not out:
        return None
    fields = out.rstrip('\n').split(SEP)
    if len(fields) != len(PLAYERCTL_FIELDS):
        return None
    status, title, artist, album, album_artist, track, length, position, art_url, player = fields
    if not title or status not in ('Playing', 'Paused'):
        return None
    shuffle = (run(['playerctl', 'shuffle']) or '').strip()
    loop = (run(['playerctl', 'loop']) or '').strip()
    length, position = as_int(length), as_int(position)          # microseconds
    return {
        'title': title, 'artist': artist, 'album': album, 'album_artist': album_artist,
        'track_number': as_int(track), 'app': player.capitalize() if player else None,
        'playing': status == 'Playing',
        'position_ms': position // 1000 if position is not None else None,
        'duration_ms': length // 1000 if length else None,
        'shuffle': {'On': True, 'Off': False}.get(shuffle), 'repeat': loop or None,
        'cover': lambda: read_url(art_url),
    }


def read_url(url):
    if not url:
        return None
    try:
        if url.startswith('file://'):
            with open(urllib.parse.unquote(url[7:]), 'rb') as f:
                return f.read(MAX_COVER + 1)
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.read(MAX_COVER + 1)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ macOS

def mac_snapshot():
    out = run(['nowplaying-cli', 'get', 'title', 'artist', 'album', 'duration', 'elapsedTime', 'playbackRate'])
    if out:
        lines = [None if l in ('null', '') else l for l in out.split('\n')[:6]]
        if len(lines) == 6 and lines[0]:
            title, artist, album, duration, elapsed, rate = lines
            return {
                'title': title, 'artist': artist, 'album': album,
                'playing': (as_int(float(rate) * 1000) or 0) > 0 if rate else False,
                'position_ms': as_int(float(elapsed) * 1000) if elapsed else None,
                'duration_ms': as_int(float(duration) * 1000) if duration else None,
                'cover': mac_cover,
            }
    return mac_applescript_snapshot()


def mac_cover():
    data = run(['nowplaying-cli', 'get', 'artworkData'], timeout=10)
    if not data or data.strip() == 'null':
        return None
    try:
        return base64.b64decode(data.strip())
    except ValueError:
        return None


# One script per app, run only while that app is running: compiling a `tell` for an app
# that isn't installed makes macOS ask "Where is Spotify?". Spotify reports duration in
# ms, Music in seconds; numbers go out as integers (a decimal comma would break parsing).
APPLESCRIPTS = {
    'Spotify': '''tell application "Spotify"
  if player state is stopped then return ""
  set t to current track
  return (player state as string) & tab & (name of t) & tab & (artist of t) & tab & (album of t) & tab & ((duration of t) as integer) & tab & ((player position * 1000) as integer) & tab & (artwork url of t)
end tell''',
    'Music': '''tell application "Music"
  if player state is stopped then return ""
  set t to current track
  return (player state as string) & tab & (name of t) & tab & (artist of t) & tab & (album of t) & tab & ((duration of t * 1000) as integer) & tab & ((player position * 1000) as integer) & tab & ""
end tell''',
}


def mac_applescript_snapshot():
    for app, script in APPLESCRIPTS.items():
        if run(['pgrep', '-x', app]) is None:
            continue
        fields = (run(['osascript', '-e', script]) or '').rstrip('\n').split('\t')
        if len(fields) == 7 and fields[1]:
            break
    else:
        return None
    state, title, artist, album, duration, position, art_url = fields
    return {
        'title': title, 'artist': artist, 'album': album, 'app': app, 'playing': state == 'playing',
        'position_ms': as_int(position), 'duration_ms': as_int(duration) or None,
        'cover': lambda: read_url(art_url),
    }


# ------------------------------------------------------------------ system stats

def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def first_hwmon(names, sensor='temp1_input'):
    """Temperature (°C) from the first hwmon device with one of these driver names."""
    for d in sorted(glob.glob('/sys/class/hwmon/hwmon*')):
        if (read_file(d + '/name') or '').strip() in names:
            value = as_int(read_file('%s/%s' % (d, sensor)))
            if value is not None:
                return value / 1000.0
    return None


class Stats:
    """CPU, memory, GPU and network. CPU and network are rates since the previous read."""

    def __init__(self):
        self.last_cpu = None    # (busy, total) jiffies
        self.last_net = None    # (monotonic, rx bytes, tx bytes)
        self.nvidia = shutil.which('nvidia-smi')

    def read(self):
        stats = {}
        if sys.platform == 'darwin':
            self.mac(stats)
        else:
            self.linux(stats)
        self.gpu(stats)
        return stats

    def linux(self, stats):
        fields = (read_file('/proc/stat') or 'cpu').split('\n')[0].split()[1:9]
        if len(fields) == 8:
            jiffies = [int(x) for x in fields]
            total, busy = sum(jiffies), sum(jiffies) - jiffies[3] - jiffies[4]    # minus idle and iowait
            if self.last_cpu and total > self.last_cpu[1]:
                stats['cpu'] = round(100.0 * (busy - self.last_cpu[0]) / (total - self.last_cpu[1]), 1)
            self.last_cpu = (busy, total)
        temp = first_hwmon(('k10temp', 'coretemp', 'zenpower', 'cpu_thermal'))
        if temp is not None:
            stats['cpu_temp'] = round(temp)
        mem = {}
        for line in (read_file('/proc/meminfo') or '').splitlines():
            key, _, value = line.partition(':')
            mem[key] = as_int((value.split() or [None])[0])                       # kB
        total, avail = mem.get('MemTotal'), mem.get('MemAvailable')
        if total and avail is not None:
            stats['mem_total_mb'], stats['mem_used_mb'] = total // 1024, (total - avail) // 1024
        # The network: the interfaces that hold a default route.
        routes = [l.split()[0] for l in (read_file('/proc/net/route') or '').splitlines()[1:]
                  if len(l.split()) > 1 and l.split()[1] == '00000000']
        rx = tx = 0
        for line in (read_file('/proc/net/dev') or '').splitlines()[2:]:
            name, _, data = line.partition(':')
            cols = data.split()
            if len(cols) >= 9 and (name.strip() in routes or (not routes and name.strip() != 'lo')):
                rx, tx = rx + int(cols[0]), tx + int(cols[8])
        self.net_rate(stats, rx, tx)

    def mac(self, stats):
        out = run(['ps', '-A', '-o', '%cpu='])
        if out:
            stats['cpu'] = round(min(100.0, sum(float(x) for x in out.split()) / (os.cpu_count() or 1)), 1)
        total, vm = as_int(run(['sysctl', '-n', 'hw.memsize'])), run(['vm_stat'])
        if total and vm:
            page = as_int((vm.split('page size of ')[1:] or ['4096'])[0].split()[0]) or 4096
            pages = dict((k.strip(), as_int(v.strip(' .\n'))) for k, v in
                         (l.split(':', 1) for l in vm.splitlines()[1:] if ':' in l))
            used = sum(pages.get(k) or 0 for k in ('Pages active', 'Pages wired down', 'Pages occupied by compressor'))
            stats['mem_total_mb'], stats['mem_used_mb'] = total // 1048576, used * page // 1048576
        iface = ''
        for line in (run(['route', '-n', 'get', 'default']) or '').splitlines():
            if line.strip().startswith('interface:'):
                iface = line.split(':', 1)[1].strip()
        for line in (run(['netstat', '-ibn']) or '').splitlines()[1:]:
            cols = line.split()
            if len(cols) >= 10 and cols[0] == iface and cols[2].startswith('<Link'):
                self.net_rate(stats, int(cols[6]), int(cols[9]))
                break

    def net_rate(self, stats, rx, tx):
        now = time.monotonic()
        if self.last_net and now > self.last_net[0]:
            seconds = now - self.last_net[0]
            stats['net_rx'] = max(0, int((rx - self.last_net[1]) / seconds))     # bytes/s
            stats['net_tx'] = max(0, int((tx - self.last_net[2]) / seconds))
        self.last_net = (now, rx, tx)

    def gpu(self, stats):
        if self.nvidia:
            out = run([self.nvidia, '--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total',
                       '--format=csv,noheader,nounits'])
            fields = [as_int(x) for x in (out or '').split('\n')[0].split(',')]
            if len(fields) == 4 and None not in fields:
                stats['gpu'], stats['gpu_temp'], stats['gpu_mem_used_mb'], stats['gpu_mem_total_mb'] = fields
            return
        for card in sorted(glob.glob('/sys/class/drm/card[0-9]/device')):    # AMD
            busy = as_int(read_file(card + '/gpu_busy_percent'))
            if busy is None:
                continue
            stats['gpu'] = busy
            used, total = as_int(read_file(card + '/mem_info_vram_used')), as_int(read_file(card + '/mem_info_vram_total'))
            if used is not None and total:
                stats['gpu_mem_used_mb'], stats['gpu_mem_total_mb'] = used // 1048576, total // 1048576
            temp = first_hwmon(('amdgpu',))
            if temp is not None:
                stats['gpu_temp'] = round(temp)
            return


# ------------------------------------------------------------------ calendars

# Each line is an iCal address, such as Google Calendar's "Secret address in iCal format".
# The Car Thing reads the files itself (repeats, time zones); this only downloads them.
CALENDAR_FILE = os.environ.get('STREAMTHING_CALENDARS') or os.path.expanduser(
    '~/Library/Application Support/streamthing/calendars.txt' if sys.platform == 'darwin'
    else '~/.config/streamthing/calendars.txt')
# Meeting links the Car Thing may ask to open: https links of known meeting services.
MEETING_URL = re.compile(r'^https://(meet\.google\.com|([a-z0-9-]+\.)?zoom\.us|teams\.microsoft\.com|teams\.live\.com)/')


def fetch_calendars():
    """(ics text, error text), or None when there is no calendars.txt."""
    try:
        with open(CALENDAR_FILE) as f:
            urls = [l.strip() for l in f if l.strip() and not l.strip().startswith('#')]
    except OSError:
        return None
    parts, errors = [], []
    for url in urls:
        url = re.sub('^webcal://', 'https://', url)
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                parts.append(r.read().decode('utf-8', 'replace'))
        except (OSError, ValueError) as e:
            errors.append(str(e))
    return '\n'.join(parts), '; '.join(errors)


def send_calendars(calendars):
    if calendars is None:
        query, body = '?none=1', b''
    else:
        query = '?error=' + urllib.parse.quote(calendars[1]) if calendars[1] else ''
        body = calendars[0].encode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:%d/api/calendar%s' % (PORT, query), data=body,
                                 headers={'Content-Type': 'text/calendar; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def open_link(url):
    subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


# ------------------------------------------------------------------ transport

def forward():
    return run([ADB, 'forward', 'tcp:%d' % PORT, 'tcp:8765'], timeout=10) is not None


def linux_locked():
    """The desktop's screensaver/lock over D-Bus (KDE and others, then GNOME)."""
    for dest in ('org.freedesktop.ScreenSaver', 'org.gnome.ScreenSaver'):
        out = run(['gdbus', 'call', '--session', '--dest', dest, '--object-path', '/' + dest.replace('.', '/'),
                   '--method', dest + '.GetActive'], timeout=3)
        if out is not None:
            return 'true' in out
    return False


def mac_locked():
    return '"CGSSessionScreenIsLocked" = Yes' in (run(['ioreg', '-n', 'Root', '-d1']) or '')


def heartbeat(locked):
    """Time and UTC offset (they set the Car Thing's clock) plus the lock state."""
    offset = datetime.datetime.now().astimezone().utcoffset()
    return {'time_ms': int(time.time() * 1000), 'utc_offset_min': int(offset.total_seconds() // 60),
            'locked': locked}


def send(path, body):
    req = urllib.request.Request('http://127.0.0.1:%d/api/%s' % (PORT, path),
                                 data=json.dumps(body).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode('utf-8') or '{}')


def log(*args):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), *args, file=sys.stderr, flush=True)


def main():
    snapshot = mac_snapshot if sys.platform == 'darwin' else linux_snapshot
    is_locked = mac_locked if sys.platform == 'darwin' else linux_locked
    stats = Stats()
    if '--once' in sys.argv:
        np = snapshot()
        if np:
            np.pop('cover')
        print(json.dumps(np, indent=2, ensure_ascii=False) if np else 'nothing playing')
        stats.read()
        time.sleep(1)
        print(json.dumps(stats.read(), indent=2))
        return
    log('started (adb: %s, port %d)' % (ADB, PORT))
    tunnel = False
    last_signature, last_sent = None, 0.0
    cover_sent_for, cover_tries = '', 0
    last_host, last_host_sent = None, 0.0
    last_stats = 0.0
    calendars, calendars_fetched, fetched_hash, calendar_hash = None, None, None, None
    while True:
        try:
            if not tunnel:
                # adb only runs at start and after a failed send (unplugged, rebooted).
                if not forward():
                    time.sleep(3)
                    continue
                tunnel = True
                last_signature, cover_sent_for, last_host, calendar_hash = None, '', None, None
            # Stats every 2 s; the reply may ask to open a meeting link or to resend the calendars.
            if time.monotonic() - last_stats >= 2:
                last_stats = time.monotonic()
                reply = send('stats', stats.read())
                link = reply.get('open')
                if link and MEETING_URL.match(link):
                    log('opening', link.split('?')[0])
                    open_link(link)
                if reply.get('need_calendar'):
                    calendar_hash = None
            if calendars_fetched is None or time.monotonic() - calendars_fetched >= 300:
                calendars_fetched = time.monotonic()
                calendars = fetch_calendars()
                if calendars and calendars[1]:
                    log('calendar:', calendars[1])
                fetched_hash = hashlib.md5(repr(calendars).encode('utf-8')).hexdigest()
            if fetched_hash != calendar_hash:
                send_calendars(calendars)
                calendar_hash = fetched_hash
            locked = is_locked()
            offset_now = heartbeat(locked)['utc_offset_min']
            if (locked, offset_now) != last_host or time.monotonic() - last_host_sent >= 10:
                send('host', heartbeat(locked))
                last_host, last_host_sent = (locked, offset_now), time.monotonic()
            np = snapshot()
            if np:
                key = hashlib.md5('|'.join(str(np.get(k) or '') for k in
                                           ('title', 'artist', 'album', 'app')).encode('utf-8')).hexdigest()
                signature = '%s|%s|%s|%s' % (key, np['playing'], np.get('duration_ms'), np.get('shuffle'))
                interval = 2 if np['playing'] else 5
            else:
                key, signature, interval = '', 'none', 10
            if signature != last_signature or time.monotonic() - last_sent >= interval:
                if np:
                    body = {k: v for k, v in np.items() if k != 'cover'}
                    body['art_key'] = key
                    if key != cover_sent_for:
                        if signature != last_signature:
                            cover_tries = 0
                        if cover_tries < 5:         # covers often appear a moment after the title
                            cover_tries += 1
                            cover = np['cover']()
                            if cover and len(cover) <= MAX_COVER:
                                body['art'] = base64.b64encode(cover).decode('ascii')
                                body['art_type'] = 'image/png' if cover[:4] == b'\x89PNG' else 'image/jpeg'
                else:
                    body = {'title': None}
                reply = send('nowplaying', body)
                if 'art' in body:
                    cover_sent_for = key
                if reply.get('need_art'):
                    cover_sent_for = ''
                last_signature, last_sent = signature, time.monotonic()
        except (OSError, ValueError) as e:
            log('update failed:', e)
            tunnel = False
            time.sleep(2)
        time.sleep(1)


if __name__ == '__main__':
    main()
