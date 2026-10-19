#!/usr/bin/env python3
"""thingdeck agent for Linux and macOS.

Sends the Car Thing the computer's time (for its clock) and lock state, system stats
(CPU, memory, GPU, network), the focused app, the volume and audio devices, the
calendars listed in calendars.txt, and what it is playing, over USB through
`adb forward`. It also opens meeting links, kills the focused app and switches audio
devices when the Car Thing asks. What is playing comes from:
  Linux  playerctl (any MPRIS player: Spotify, browsers, VLC, ...)
  macOS  nowplaying-cli; if that returns nothing (macOS 15.4+ restricts the API it
         uses), AppleScript for Spotify and Music

  python3 nowplaying.py          # run
  python3 nowplaying.py --once   # print one snapshot

install.sh sets it up to start at login. Environment: ADB (path to adb),
THINGDECK_PORT (host side of the forward, default 28765), THINGDECK_CALENDARS
(calendars.txt; default ~/.config/thingdeck/ on Linux,
~/Library/Application Support/thingdeck/ on macOS).
"""
import ast
import base64
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

PORT = int(os.environ.get('THINGDECK_PORT', '28765'))   # the device listens on 8765
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


# ------------------------------------------------------------------ focused app (kill switch)

# Never offered for killing: the session, the compositor or window server, and this agent.
PROTECTED = {'systemd', 'init', 'gnome-shell', 'gnome-session-b', 'mutter', 'kwin_wayland', 'kwin_x11', 'plasmashell',
             'xorg', 'xwayland', 'sway', 'hyprland', 'gdm', 'sddm', 'finder', 'dock', 'windowserver', 'loginwindow',
             'systemuiserver', 'controlcenter'}


def process_name(pid):
    if sys.platform == 'darwin':
        return (run(['ps', '-p', str(pid), '-o', 'comm=']) or '').strip().rsplit('/', 1)[-1] or None
    return (read_file('/proc/%d/comm' % pid) or '').strip() or None


def window_from_tree(node):
    """Sway's focused window in its layout tree."""
    if node.get('focused') and node.get('pid'):
        return node
    for child in node.get('nodes', []) + node.get('floating_nodes', []):
        found = window_from_tree(child)
        if found:
            return found
    return None


def linux_focus():
    """(pid, window title) of the focused window, or (None, why not)."""
    env = os.environ
    if env.get('HYPRLAND_INSTANCE_SIGNATURE'):
        window = json.loads(run(['hyprctl', 'activewindow', '-j']) or '{}')
        return window.get('pid'), window.get('title', '')
    if env.get('SWAYSOCK'):
        window = window_from_tree(json.loads(run(['swaymsg', '-t', 'get_tree']) or '{}')) or {}
        return window.get('pid'), window.get('name', '')
    desktop = env.get('XDG_CURRENT_DESKTOP', '').upper()
    wayland = env.get('XDG_SESSION_TYPE') == 'wayland' or env.get('WAYLAND_DISPLAY')
    if 'GNOME' in desktop and wayland:
        # GNOME keeps the focused window to itself; the "Window Calls" extension shares it.
        out = run(['gdbus', 'call', '--session', '--dest', 'org.gnome.Shell', '--object-path',
                   '/org/gnome/Shell/Extensions/Windows', '--method', 'org.gnome.Shell.Extensions.Windows.List'])
        if out is None:
            return None, 'needs the Window Calls GNOME extension'
        try:
            windows = json.loads(ast.literal_eval(out.strip())[0])
        except (ValueError, SyntaxError, IndexError):
            return None, 'Window Calls gave an unexpected answer'
        window = next((w for w in windows if w.get('focus')), {})
        return window.get('pid'), window.get('title') or window.get('wm_class') or ''
    if 'KDE' in desktop and wayland:
        if not shutil.which('kdotool'):
            return None, 'needs kdotool on KDE Wayland'
        window = (run(['kdotool', 'getactivewindow']) or '').strip()
        return as_int(run(['kdotool', 'getwindowpid', window])), (run(['kdotool', 'getwindowname', window]) or '').strip()
    if env.get('DISPLAY') and shutil.which('xprop'):
        active = re.search(r'0x[0-9a-f]+', run(['xprop', '-root', '_NET_ACTIVE_WINDOW']) or '')
        if active:
            props = run(['xprop', '-id', active.group(0), '_NET_WM_PID', '_NET_WM_NAME']) or ''
            pid, title = re.search(r'_NET_WM_PID\(CARDINAL\) = (\d+)', props), re.search(r'_NET_WM_NAME\(\w+\) = "(.*)"', props)
            return (int(pid.group(1)) if pid else None), (title.group(1) if title else '')
    return None, 'not supported on this desktop'


def mac_focus():
    """The frontmost app through lsappinfo, which needs no permission prompt."""
    front = (run(['lsappinfo', 'front']) or '').strip()
    info = run(['lsappinfo', 'info', '-only', 'pid', '-only', 'name', front]) if front else None
    pid, name = re.search(r'"pid"=(\d+)', info or ''), re.search(r'"LSDisplayName"="(.*?)"', info or '')
    return (int(pid.group(1)) if pid else None), (name.group(1) if name else '')


def focus():
    """{'pid', 'name', 'app', 'title', 'protected'} of the focused app, or {'error': why not}."""
    pid, title = mac_focus() if sys.platform == 'darwin' else linux_focus()
    if not pid:
        return {'error': title} if title else None
    name = process_name(pid)
    if not name:
        return None
    return {'pid': pid, 'name': name, 'app': (title if sys.platform == 'darwin' else '') or name,
            'title': '' if sys.platform == 'darwin' else title,
            'protected': name.lower() in PROTECTED or pid in (os.getpid(), os.getppid())}


def kill_focused(target):
    """Kill what the Car Thing showed: the same process id and still the same program."""
    result = {'name': str(target.get('app') or target.get('name')), 'ok': False}
    pid = as_int(target.get('pid'))
    name = process_name(pid) if pid else None
    if not name or name != target.get('name'):
        result['error'] = 'it is not running anymore'
    elif name.lower() in PROTECTED or pid in (os.getpid(), os.getppid()):
        result['error'] = 'it is protected'
    else:
        try:
            os.kill(pid, signal.SIGKILL)     # what Force Quit does
            log('killed %s (%d)' % (name, pid))
            result['ok'] = True
        except PermissionError:
            result['error'] = 'permission denied: it runs as another user'
        except OSError as e:
            result['error'] = str(e)
    return result


# ------------------------------------------------------------------ audio

def linux_audio(lists):
    """PipeWire or PulseAudio through pactl: the default sink's volume and mute, the
    default sink and source, and (with lists) every sink and source."""
    state = {'output': (run(['pactl', 'get-default-sink']) or '').strip() or None,
             'input': (run(['pactl', 'get-default-source']) or '').strip() or None}
    volume = re.search(r'(\d+)%', run(['pactl', 'get-sink-volume', '@DEFAULT_SINK@']) or '')
    state['volume'] = int(volume.group(1)) if volume else None
    state['muted'] = 'yes' in (run(['pactl', 'get-sink-mute', '@DEFAULT_SINK@']) or '')
    if lists:
        for kind, what in (('outputs', 'sinks'), ('inputs', 'sources')):
            try:
                devices = json.loads(run(['pactl', '-f', 'json', 'list', what]) or '[]')
            except ValueError:
                devices = []
            state[kind] = [{'id': d['name'], 'name': d.get('description') or d['name']} for d in devices
                           if d.get('name') and not d['name'].endswith('.monitor')]     # skip "monitor of" sources
    return state


def mac_audio(lists):
    """The volume through AppleScript; devices through SwitchAudioSource, if installed."""
    settings = run(['osascript', '-e', 'get volume settings']) or ''
    volume = re.search(r'output volume:(\d+)', settings)
    state = {'volume': int(volume.group(1)) if volume else None, 'muted': 'output muted:true' in settings}
    if shutil.which('SwitchAudioSource'):
        for kind, what in (('output', 'output'), ('input', 'input')):
            state[kind] = (run(['SwitchAudioSource', '-c', '-t', what]) or '').strip() or None
            if lists:
                state[kind + 's'] = [{'id': n, 'name': n} for n in (run(['SwitchAudioSource', '-a', '-t', what]) or '').splitlines() if n]
    return state


def set_audio_device(device_id, kind):
    if sys.platform == 'darwin':
        run(['SwitchAudioSource', '-t', kind, '-s', device_id])
    else:
        run(['pactl', 'set-default-sink' if kind == 'output' else 'set-default-source', device_id])


class AudioLink:
    """Sends the audio state when it changes and at least every 2 s; the reply may carry a
    device the Car Thing picked. The device lists are read every 3 s and sent when they change."""

    def __init__(self):
        self.listed, self.lists = 0.0, {}
        self.sent, self.lists_sent, self.sent_at = None, None, 0.0

    def update(self):
        fresh = time.monotonic() - self.listed >= 3
        if fresh:
            self.listed = time.monotonic()
        state = (mac_audio if sys.platform == 'darwin' else linux_audio)(fresh)
        if fresh:
            self.lists = {k: state.pop(k) for k in ('outputs', 'inputs') if k in state}
        if state.get('volume') is None and not any(self.lists.values()):
            return          # no pactl / nothing to show
        key, lists_key = json.dumps(state, sort_keys=True), json.dumps(self.lists, sort_keys=True)
        if key == self.sent and lists_key == self.lists_sent and time.monotonic() - self.sent_at < 2:
            return
        body = dict(state, **(self.lists if lists_key != self.lists_sent else {}))
        reply = send('audio', body)
        self.sent, self.lists_sent, self.sent_at = key, lists_key, time.monotonic()
        if reply.get('need_lists'):
            self.lists_sent = None
        target = reply.get('set')
        if isinstance(target, dict) and target.get('id'):
            set_audio_device(str(target['id']), 'input' if target.get('kind') == 'input' else 'output')
            self.listed = 0.0


# ------------------------------------------------------------------ calendars

# Each line is an iCal address, such as Google Calendar's "Secret address in iCal format".
# The Car Thing reads the files itself (repeats, time zones); this only downloads them.
CALENDAR_FILE = os.environ.get('THINGDECK_CALENDARS') or os.path.expanduser(
    '~/Library/Application Support/thingdeck/calendars.txt' if sys.platform == 'darwin'
    else '~/.config/thingdeck/calendars.txt')
# Links the Car Thing may ask to open: https links of known meeting services, and GitHub.
OPENABLE_URL = re.compile(
    r'^https://(meet\.google\.com|([a-z0-9-]+\.)?zoom\.us|teams\.microsoft\.com|teams\.live\.com|github\.com)/')

# ------------------------------------------------------------------ Philips Hue

# The Car Thing has no network, so the agent talks to the Hue Bridge (its local API v2) and
# passes rooms, zones, lights and scenes on. The bridge's address and key are in hue.json
# next to calendars.txt, written when you pair from the Car Thing's Lights page.
HUE_FILE = os.path.join(os.path.dirname(CALENDAR_FILE), 'hue.json')


class HueLink:
    def __init__(self):
        self.config = None             # {'bridge', 'key'}
        self.pairing = None            # {'until', 'next', 'bridge'} while waiting for the button
        self.lists, self.listed = None, 0.0
        self.fetched, self.sent, self.sent_at = 0.0, None, 0.0
        # The bridge's certificate comes from Philips' own authority, not a public one; this
        # context is used only for requests to the bridge's own address.
        self.tls = ssl.create_default_context()
        self.tls.check_hostname = False
        self.tls.verify_mode = ssl.CERT_NONE

    def load(self):
        try:
            with open(HUE_FILE) as f:
                config = json.load(f)
            if config.get('bridge') and config.get('key'):
                self.config = {'bridge': str(config['bridge']), 'key': str(config['key'])}
        except (OSError, ValueError):
            pass

    def call(self, method, path, body=None, bridge=None, key=None):
        bridge = bridge or self.config['bridge']
        key = self.config['key'] if key is None else key
        req = urllib.request.Request('https://%s%s' % (bridge, path), method=method,
                                     data=json.dumps(body).encode('utf-8') if body is not None else None,
                                     headers=dict({'Content-Type': 'application/json'}, **({'hue-application-key': key} if key else {})))
        try:
            with urllib.request.urlopen(req, timeout=5, context=self.tls) as r:
                result = json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            result = json.loads(e.read().decode('utf-8') or '{}')
        if isinstance(result, dict) and result.get('errors'):
            raise OSError('Hue: ' + result['errors'][0].get('description', 'error'))
        return result

    def send(self, state):
        send('hue', state)
        self.sent, self.sent_at = json.dumps(state, sort_keys=True), time.monotonic()

    def step_pairing(self):
        p = self.pairing
        if time.monotonic() < p['next']:
            return
        if time.monotonic() > p['until']:
            self.pairing = None
            self.send({'state': 'error', 'error': "The bridge's button wasn't pressed. Tap Pair, then press it within 30 seconds."})
            return
        p['next'] = time.monotonic() + 2
        if not p['bridge']:
            with urllib.request.urlopen('https://discovery.meethue.com/', timeout=10) as r:
                found = [b for b in json.loads(r.read().decode('utf-8')) if b.get('internalipaddress')]
            if not found:
                raise OSError('No Hue Bridge found on the network')
            p['bridge'] = found[0]['internalipaddress']
        answer = self.call('POST', '/api', {'devicetype': 'thingdeck#' + socket.gethostname()[:19], 'generateclientkey': True},
                           bridge=p['bridge'], key='')
        success = answer[0].get('success') if isinstance(answer, list) and answer else None
        if success and success.get('username'):
            os.makedirs(os.path.dirname(HUE_FILE), exist_ok=True)
            with open(HUE_FILE, 'w') as f:
                json.dump({'bridge': p['bridge'], 'key': success['username']}, f)
            os.chmod(HUE_FILE, 0o600)
            log('hue: paired with the bridge at', p['bridge'])
            self.pairing, self.fetched, self.listed = None, 0.0, 0.0
            self.load()

    def read(self):
        """Rooms and zones with their lights and scenes; the structure every 30 s, levels every call."""
        if self.lists is None or time.monotonic() - self.listed >= 30:
            self.lists = {'rooms': self.call('GET', '/clip/v2/resource/room')['data'] + self.call('GET', '/clip/v2/resource/zone')['data'],
                          'devices': self.call('GET', '/clip/v2/resource/device')['data'],
                          'scenes': self.call('GET', '/clip/v2/resource/scene')['data']}
            self.listed = time.monotonic()
        lights = {l['id']: l for l in self.call('GET', '/clip/v2/resource/light')['data']}
        groups = {g['id']: g for g in self.call('GET', '/clip/v2/resource/grouped_light')['data']}
        device_lights = {d['id']: [s['rid'] for s in d.get('services', []) if s['rtype'] == 'light'] for d in self.lists['devices']}
        rooms = []
        for r in self.lists['rooms']:
            group_id = next((s['rid'] for s in r.get('services', []) if s['rtype'] == 'grouped_light'), None)
            ids = []
            for c in r.get('children', []):
                ids += [c['rid']] if c['rtype'] == 'light' else device_lights.get(c['rid'], []) if c['rtype'] == 'device' else []
            group = groups.get(group_id) or {}
            rooms.append({
                'id': r['id'], 'kind': r['type'], 'name': r['metadata']['name'], 'archetype': r['metadata'].get('archetype'),
                'group': group_id, 'on': bool(group.get('on', {}).get('on')),
                'brightness': round(group['dimming']['brightness'], 1) if group.get('dimming') else None,
                'lights': [{'id': l['id'], 'name': l['metadata']['name'], 'on': bool(l['on']['on']),
                            'brightness': round(l['dimming']['brightness'], 1) if l.get('dimming') else None,
                            'xy': [l['color']['xy']['x'], l['color']['xy']['y']] if l.get('color') else None,
                            'mirek': l['color_temperature']['mirek'] if (l.get('color_temperature') or {}).get('mirek_valid') else None,
                            'ct': bool(l.get('color_temperature'))}
                           for l in (lights.get(i) for i in ids) if l],
                'scenes': [{'id': s['id'], 'name': s['metadata']['name'],
                            'active': s.get('status', {}).get('active', 'inactive') != 'inactive'}
                           for s in self.lists['scenes'] if s.get('group', {}).get('rid') == r['id']]})
        rooms.sort(key=lambda r: (r['kind'] != 'room', r['name']))
        return {'state': 'ok', 'rooms': rooms}

    def run(self, cmd):
        op = cmd.get('op')
        if op == 'pair':
            self.pairing = {'until': time.monotonic() + 30, 'next': 0.0, 'bridge': None}
            self.send({'state': 'pairing'})
            return
        if not self.config:
            raise OSError('Pair the Hue Bridge first')
        if op == 'scene':
            self.call('PUT', '/clip/v2/resource/scene/%s' % cmd['id'], {'recall': {'action': 'active'}})
        elif op == 'all_off':
            for r in self.lists['rooms']:
                group = next((s['rid'] for s in r.get('services', []) if s['rtype'] == 'grouped_light'), None)
                if group and r['type'] == 'room':
                    self.call('PUT', '/clip/v2/resource/grouped_light/%s' % group, {'on': {'on': False}})
        elif op in ('group', 'light'):
            body = {}
            if cmd.get('on') is not None:
                body['on'] = {'on': bool(cmd['on'])}
            if cmd.get('brightness') is not None:
                body['dimming'] = {'brightness': max(1.0, float(cmd['brightness']))}
            if body:
                self.call('PUT', '/clip/v2/resource/%s/%s' % ('grouped_light' if op == 'group' else 'light', cmd['id']), body)
            if cmd.get('mirek') is not None:          # white temperature: per light, on the lights that have it
                room = next((r for r in self.read()['rooms'] if r['group'] == cmd['id']), None)
                for l in (room or {}).get('lights', []):
                    if l['ct']:
                        self.call('PUT', '/clip/v2/resource/light/%s' % l['id'], {'color_temperature': {'mirek': int(cmd['mirek'])}})
        self.fetched = 0.0           # show the result right away

    def update(self):
        """Commands from the Car Thing, pairing, and the lights' state every 2 s."""
        with urllib.request.urlopen('http://127.0.0.1:%d/api/commands' % PORT, timeout=5) as r:
            commands = json.loads(r.read().decode('utf-8')).get('hue') or []
        if not self.config and not self.pairing:
            self.load()
        for cmd in commands:
            try:
                self.run(cmd)
            except (OSError, ValueError, KeyError) as e:
                log('hue:', e)
                self.send({'state': 'error', 'error': str(e)})
        if self.pairing:
            try:
                self.step_pairing()
            except (OSError, ValueError, KeyError) as e:
                log('hue pairing:', e)
                self.pairing = None
                self.send({'state': 'error', 'error': str(e)})
            return
        if time.monotonic() - self.fetched < 2:
            return
        self.fetched = time.monotonic()
        if not self.config:
            state = {'state': 'none'}
        else:
            try:
                state = self.read()
            except (OSError, ValueError, KeyError) as e:
                self.fetched = time.monotonic() + 28       # bridge unreachable: retry in 30 s
                state = {'state': 'error', 'error': str(e)}
        if json.dumps(state, sort_keys=True) != self.sent or time.monotonic() - self.sent_at >= 10:
            self.send(state)


# ------------------------------------------------------------------ GitHub

# A repo's latest workflow runs and your open pull requests, through the gh CLI, which
# keeps the login (nothing is stored here). The repo is the first line of github.txt next
# to calendars.txt ("name" for one of yours, or "owner/name").
GITHUB_FILE = os.path.join(os.path.dirname(CALENDAR_FILE), 'github.txt')
PR_QUERY = ('query { viewer { pullRequests(states: OPEN, first: 20, orderBy: {field: UPDATED_AT, direction: DESC}) '
            '{ nodes { number title url isDraft updatedAt reviewDecision repository { nameWithOwner } '
            'commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } } } } }')


def gh(*args, stdin=None):
    out = subprocess.run(['gh'] + list(args), input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         timeout=30, universal_newlines=True)
    if out.returncode != 0:
        raise OSError((out.stderr.strip().split('\n') or ['gh failed'])[0])
    return out.stdout


class GitHub:
    def __init__(self):
        self.name, self.full = '', ''      # the github.txt line and what it resolved to

    def read(self):
        try:
            with open(GITHUB_FILE) as f:
                name = next((l.strip() for l in f if l.strip() and not l.strip().startswith('#')), '')
        except OSError:
            name = ''
        if not name:
            return {'none': True}
        result = {'repo': name, 'runs': [], 'prs': []}
        if not shutil.which('gh'):
            result['error'] = 'Install the GitHub CLI (gh) and sign in (see README)'
            return result
        try:
            if self.name != name:              # "name" alone: one of the signed-in account's repos
                full = name if '/' in name else gh('repo', 'view', name, '--json', 'nameWithOwner', '-q', '.nameWithOwner').strip()
                self.name, self.full = name, full
            result['repo'] = self.full
            seen = set()
            for run in json.loads(gh('run', 'list', '-R', self.full, '-L', '100', '--json',
                                     'workflowName,displayTitle,status,conclusion,headBranch,event,createdAt,updatedAt,url')):
                if run['workflowName'] in seen or len(result['runs']) == 20:    # the newest run of each workflow
                    continue
                seen.add(run['workflowName'])
                result['runs'].append({'workflow': run['workflowName'], 'status': run['status'], 'conclusion': run['conclusion'],
                                       'branch': run['headBranch'], 'title': run['displayTitle'], 'event': run['event'],
                                       'created': run['createdAt'], 'updated': run['updatedAt'], 'url': run['url']})
            prs = json.loads(gh('api', 'graphql', '--input', '-', stdin=json.dumps({'query': PR_QUERY})))
            for pr in prs['data']['viewer']['pullRequests']['nodes']:
                commits = pr['commits']['nodes']
                rollup = commits[0]['commit']['statusCheckRollup'] if commits else None
                result['prs'].append({'repo': pr['repository']['nameWithOwner'], 'number': pr['number'], 'title': pr['title'],
                                      'draft': pr['isDraft'], 'review': pr['reviewDecision'], 'updated': pr['updatedAt'],
                                      'url': pr['url'], 'checks': rollup['state'] if rollup else None})
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as e:
            result['error'] = str(e)
        return result


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
        print(json.dumps(focus(), indent=2, ensure_ascii=False))
        print(json.dumps((mac_audio if sys.platform == 'darwin' else linux_audio)(True), indent=2, ensure_ascii=False))
        return
    log('started (adb: %s, port %d)' % (ADB, PORT))
    tunnel = False
    last_signature, last_sent = None, 0.0
    cover_sent_for, cover_tries = '', 0
    last_host, last_host_sent = None, 0.0
    last_stats, kill_result = 0.0, None
    audio = AudioLink()
    hue = HueLink()
    calendars, calendars_fetched, fetched_hash, calendar_hash = None, None, None, None
    github, github_fetched, github_busy = GitHub(), None, False
    while True:
        try:
            if not tunnel:
                # adb only runs at start and after a failed send (unplugged, rebooted).
                if not forward():
                    time.sleep(3)
                    continue
                tunnel = True
                last_signature, cover_sent_for, last_host, calendar_hash = None, '', None, None
            # Stats (and the focused app) every 2 s; the reply may ask to kill that app, open a
            # meeting link or resend the calendars.
            if time.monotonic() - last_stats >= 2:
                last_stats = time.monotonic()
                body = stats.read()
                body['focus'] = focus()
                if kill_result:
                    body['kill_result'], kill_result = kill_result, None
                reply = send('stats', body)
                if reply.get('kill'):
                    kill_result, last_stats = kill_focused(reply['kill']), 0.0     # report it right away
                link = reply.get('open')
                if link and OPENABLE_URL.match(link):
                    log('opening', link.split('?')[0])
                    open_link(link)
                if reply.get('need_calendar'):
                    calendar_hash = None
            # GitHub every minute, or every 15 s while a workflow runs.
            if github_fetched is None or time.monotonic() - github_fetched >= (15 if github_busy else 60):
                github_fetched = time.monotonic()
                status = github.read()
                github_busy = any(r['status'] != 'completed' for r in status.get('runs', []))
                send('github', status)
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
            audio.update()
            hue.update()
        except (OSError, ValueError) as e:
            log('update failed:', e)
            tunnel = False
            time.sleep(2)
        time.sleep(1)


if __name__ == '__main__':
    main()
