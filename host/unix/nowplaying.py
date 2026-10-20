#!/usr/bin/env python3
"""thingdeck agent for Linux and macOS.

Sends the Car Thing the computer's time (for its clock) and lock state, system stats
(CPU, memory, GPU, network), the focused app, the volume and audio devices, the
calendars listed in calendars.txt, the Plex server in plex.txt, the TrueNAS server in truenas.txt,
and what it is playing, over USB through
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
# This script's fingerprint: deploy.sh puts the repo's on the Car Thing, which says when they differ.
with open(os.path.abspath(__file__), 'rb') as _f:
    AGENT_VERSION = hashlib.sha1(_f.read()).hexdigest()[:12]


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


# The live panels on the Car Thing's screen ("lights", "monitor", "github", "plex", …), from its
# command poll (HueLink.update). Those update often; the others slowly, which saves the work.
visible = set()


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
        self.gpu_at, self.gpu_last = 0.0, {}       # the last nvidia-smi read

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
            # Counted from the end: the Address column is empty for a VPN's utun, shifting the rest.
            if len(cols) >= 10 and cols[0] == iface and cols[2].startswith('<Link'):
                self.net_rate(stats, int(cols[-5]), int(cols[-2]))
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
            # nvidia-smi is the costly part: every 2 s while Status is on screen, else every 30 s.
            if 'monitor' in visible or time.monotonic() - self.gpu_at >= 30:
                self.gpu_at = time.monotonic()
                out = run([self.nvidia, '--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total',
                           '--format=csv,noheader,nounits'])
                fields = [as_int(x) for x in (out or '').split('\n')[0].split(',')]
                self.gpu_last = dict(zip(('gpu', 'gpu_temp', 'gpu_mem_used_mb', 'gpu_mem_total_mb'), fields)) \
                    if len(fields) == 4 and None not in fields else {}
            stats.update(self.gpu_last)
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
    info = (run(['lsappinfo', 'info', '-only', 'pid', '-only', 'name', front]) if front else None) or ''
    # Older macOS prints "pid"=123 and "LSDisplayName"="Finder"; newer ones the full record
    # ("Finder" ASN:0x0-0x22022: … pid = 123 …) whatever -only asks for.
    pid = re.search(r'"?pid"?\s*=\s*(\d+)', info)
    name = re.search(r'"LSDisplayName"="(.*?)"', info) or re.search(r'^"(.*?)" ASN:', info, re.M)
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
        self.github_refresh = False    # set from the command poll, cleared by the main loop
        self.opened = set()            # panels that just came on screen, for the main loop
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
            polled = json.loads(r.read().decode('utf-8'))
        commands = polled.get('hue') or []
        self.github_refresh = self.github_refresh or bool(polled.get('github_refresh'))   # the Code page's refresh button
        now_visible = set(polled.get('visible') or [])
        self.opened |= now_visible - visible             # just came on screen: the main loop reads them now
        visible.clear()
        visible.update(now_visible)
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
        if time.monotonic() - self.fetched < (2 if 'lights' in visible else 30):
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
# One request for the last 100 runs (gh run list makes a second one for the workflow names).
RUN_FIELDS = ('[.workflow_runs[] | {workflow_id, name, display_title, status, conclusion, head_branch, event, '
              'created_at, updated_at, html_url}]')


def gh(*args, stdin=None):
    out = subprocess.run(['gh'] + list(args), input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         timeout=30, universal_newlines=True)
    if out.returncode != 0:
        raise OSError((out.stderr.strip().split('\n') or ['gh failed'])[0])
    return out.stdout


class GitHub:
    def __init__(self):
        self.name, self.full = '', ''      # the github.txt line and what it resolved to
        self.workflows, self.workflows_at = {}, None     # workflow id -> name

    def workflow_names(self, runs):
        """A run's own "name" is its run-name, which a workflow can set per run, so the names
        come from the workflow list: read once an hour, or when a run has a workflow not seen
        yet (each new id is looked up only once; a deleted workflow keeps its run's name)."""
        unknown = any(r['workflow_id'] not in self.workflows for r in runs)
        if unknown or self.workflows_at is None or time.monotonic() - self.workflows_at >= 3600:
            self.workflows = {w['id']: w['name'] for w in json.loads(gh(
                'api', 'repos/%s/actions/workflows?per_page=100' % self.full, '--jq', '[.workflows[] | {id, name}]'))}
            self.workflows_at = time.monotonic()
            for r in runs:
                self.workflows.setdefault(r['workflow_id'], r['name'])
        return self.workflows

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
                self.workflows, self.workflows_at = {}, None
            result['repo'] = self.full
            runs = json.loads(gh('api', 'repos/%s/actions/runs?per_page=100&exclude_pull_requests=true' % self.full,
                                 '--jq', RUN_FIELDS))
            names, seen = self.workflow_names(runs), set()
            for run in runs:
                if run['workflow_id'] in seen or len(result['runs']) == 20:     # the newest run of each workflow
                    continue
                seen.add(run['workflow_id'])
                result['runs'].append({'workflow': names[run['workflow_id']], 'status': run['status'],
                                       'conclusion': run['conclusion'] or '', 'branch': run['head_branch'],
                                       'title': run['display_title'], 'event': run['event'], 'created': run['created_at'],
                                       'updated': run['updated_at'], 'url': run['html_url']})
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


# ------------------------------------------------------------------ Plex

# A Plex Media Server on the local network: its streams, load, bandwidth and libraries. The
# first line of plex.txt next to calendars.txt is the server's address (192.168.1.20, or
# http://192.168.1.20:32400); a second line holds a Plex token, for a server that asks for
# one on the local network.
PLEX_FILE = os.path.join(os.path.dirname(CALENDAR_FILE), 'plex.txt')
PLEX_STEP = 6           # seconds per sample in the server's own load and bandwidth history
PLEX_HISTORY = 60       # samples sent for the graphs
RESOLUTIONS = {'4k': '4K', 'sd': 'SD'}


def plex_session(s):
    """One stream from /status/sessions, as the Car Thing shows it, plus its poster's path."""
    kind, player, session = s.get('type'), s.get('Player') or {}, s.get('Session') or {}
    media, transcode = (s.get('Media') or [{}])[0], s.get('TranscodeSession')
    if kind == 'episode':
        number = ('S%s · E%s' % (s['parentIndex'], s['index'])
                  if s.get('parentIndex') is not None and s.get('index') is not None else '')
        title, sub = s.get('grandparentTitle'), ' · '.join(x for x in (number, s.get('title')) if x)
        thumb = s.get('grandparentThumb') or s.get('thumb')
    elif kind == 'track':
        title, sub = s.get('title'), ' — '.join(x for x in (s.get('grandparentTitle'), s.get('parentTitle')) if x)
        thumb = s.get('parentThumb') or s.get('thumb')
    else:
        title, sub, thumb = s.get('title'), str(s.get('year') or ''), s.get('thumb')
    if not transcode:
        decision = 'direct play'
    elif transcode.get('videoDecision') == 'transcode' or (kind == 'track' and transcode.get('audioDecision') == 'transcode'):
        decision = 'transcode'
    else:
        decision = 'direct stream'      # the video is copied; only the audio or the container changes
    resolution = str(media.get('videoResolution') or '')
    quality = (RESOLUTIONS.get(resolution.lower(), resolution + 'p' if resolution.isdigit() else resolution)
               if resolution else str(media.get('audioCodec') or '').upper())
    location = session.get('location')
    return {'id': str(session.get('id') or s.get('sessionKey') or ''), 'kind': kind, 'title': title, 'sub': sub,
            'user': (s.get('User') or {}).get('title'), 'player': player.get('title') or player.get('product'),
            'state': player.get('state'), 'position_ms': as_int(s.get('viewOffset')),
            'duration_ms': as_int(s.get('duration')), 'decision': decision,
            'hw': bool(transcode and (transcode.get('transcodeHwEncoding') or transcode.get('transcodeHwFullPipeline'))),
            'quality': quality, 'kbps': as_int(session.get('bandwidth')),
            'local': location != 'wan' if location else player.get('local') is not False, 'thumb': thumb}


class PlexLink:
    """Reads the server every 3 s and sends it to the Car Thing; the library counts and the
    update check every 5 minutes. When the server doesn't answer, it tries again in 30 s.
    Posters go only when the Car Thing asks for them (need_art in its reply)."""

    def __init__(self):
        self.server = self.token = None
        self.next = 0.0                                  # monotonic time of the next read
        self.counted, self.libraries, self.update_version = None, [], None
        self.thumbs, self.need = {}, []                  # poster key -> (path, square); keys asked for
        self.sent, self.sent_at, self.error = None, 0.0, None

    def load(self):
        try:
            with open(PLEX_FILE) as f:
                lines = [l.strip() for l in f if l.strip() and not l.strip().startswith('#')]
        except OSError:
            lines = []
        server = lines[0].rstrip('/') if lines else None
        if server and '://' not in server:
            server = 'http://' + server
        if server and urllib.parse.urlsplit(server).port is None:
            server += ':32400'
        if server != self.server:
            self.counted, self.libraries = None, []
        self.server, self.token = server, (lines[1] if len(lines) > 1 else None)

    def get(self, path, raw=False):
        headers = dict({'Accept': 'application/json'}, **({'X-Plex-Token': self.token} if self.token else {}))
        with urllib.request.urlopen(urllib.request.Request(self.server + path, headers=headers), timeout=4) as r:
            data = r.read(MAX_COVER + 1) if raw else r.read()
        return data if raw else json.loads(data.decode('utf-8') or '{}').get('MediaContainer') or {}

    def optional(self, path):
        """A Plex Pass part (load, bandwidth, updates): None when the server doesn't give it."""
        try:
            return self.get(path)
        except urllib.error.HTTPError:
            return None

    def count_libraries(self):
        libraries = []
        for d in self.get('/library/sections').get('Directory') or []:
            def total(kind=None):
                query = ('type=%d&' % kind if kind else '') + 'X-Plex-Container-Start=0&X-Plex-Container-Size=0'
                return as_int(self.get('/library/sections/%s/all?%s' % (d['key'], query)).get('totalSize'))
            library = {'title': d.get('title'), 'kind': d.get('type'), 'count': total()}
            if d.get('type') == 'show':
                library['episodes'] = total(4)
            elif d.get('type') == 'artist':
                library['albums'], library['tracks'] = total(9), total(10)
            libraries.append(library)
        releases = (self.optional('/updater/status') or {}).get('Release') or []
        self.update_version = next((r['version'] for r in releases if r.get('version')), None)
        self.libraries, self.counted = libraries, time.monotonic()

    def history(self):
        """The server's own load and bandwidth history, for the graphs (needs Plex Pass)."""
        rows = sorted((self.optional('/statistics/resources?timespan=%d' % PLEX_STEP) or {}).get('StatisticsResources') or [],
                      key=lambda r: r.get('at') or 0)[-PLEX_HISTORY:]
        load = {'cpu': [round(r.get('hostCpuUtilization') or 0, 1) for r in rows],
                'mem': [round(r.get('hostMemoryUtilization') or 0, 1) for r in rows],
                'plex_cpu': rows[-1].get('processCpuUtilization'),
                'plex_mem': rows[-1].get('processMemoryUtilization')} if rows else None
        bandwidth = self.optional('/statistics/bandwidth?timespan=%d' % PLEX_STEP)
        if bandwidth is None:
            return load, None
        # One row per device and sample, at uneven times: add them up per 6 s, LAN and WAN apart.
        buckets = {}
        for r in bandwidth.get('StatisticsBandwidth') or []:
            bucket = buckets.setdefault((as_int(r.get('at')) or 0) // PLEX_STEP, [0, 0])
            bucket[0 if r.get('lan') else 1] += as_int(r.get('bytes')) or 0
        end = max([int(time.time()) // PLEX_STEP] + list(buckets))
        start = max(min(buckets or [end]), end - PLEX_HISTORY + 1)
        steps = [buckets.get(k, [0, 0]) for k in range(start, end + 1)]
        return load, {'lan': [b[0] // PLEX_STEP for b in steps], 'wan': [b[1] // PLEX_STEP for b in steps]}

    def read(self):
        self.load()
        if not self.server:
            return {'state': 'none'}
        if self.counted is None or time.monotonic() - self.counted >= 300:
            self.count_libraries()
        root = self.get('/')
        sessions, thumbs = [], {}
        for s in self.get('/status/sessions').get('Metadata') or []:
            session = plex_session(s)
            thumb = session.pop('thumb')
            if thumb:
                session['art'] = hashlib.md5(thumb.encode('utf-8')).hexdigest()[:16]
                thumbs[session['art']] = (thumb, session['kind'] == 'track')
            sessions.append(session)
        self.thumbs = thumbs
        load, net = self.history()
        activities = [{'title': a.get('title'), 'sub': a.get('subtitle'), 'progress': as_int(a.get('progress'))}
                      for a in self.get('/activities').get('Activity') or [] if a.get('title')]
        version = root.get('version') or ''
        return {'state': 'ok', 'sessions': sessions, 'load': load, 'net': net, 'libraries': self.libraries,
                'activities': activities,
                'server': {'name': root.get('friendlyName'), 'version': version,
                           'update': self.update_version if self.update_version not in (None, version) else None,
                           'transcodes': as_int(root.get('transcoderActiveVideoSessions')) or 0}}

    def posters(self):
        """The posters the Car Thing asked for, small (it shows them 44 px wide)."""
        posters = {}
        for key in self.need:
            if key not in self.thumbs:
                continue
            thumb, square = self.thumbs[key]
            query = urllib.parse.urlencode({'width': 144 if square else 96, 'height': 144, 'minSize': 1, 'upscale': 1, 'url': thumb})
            try:
                data = self.get('/photo/:/transcode?' + query, raw=True)
            except OSError as e:
                log('plex poster:', e)
                continue
            if data and len(data) <= MAX_COVER:
                posters[key] = base64.b64encode(data).decode('ascii')
        return posters

    def update(self):
        if time.monotonic() < self.next:
            return
        self.next = time.monotonic() + (3 if 'plex' in visible else 30)
        try:
            state = self.read()
            if self.need:
                state['posters'] = self.posters()
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.next = time.monotonic() + 30           # server unreachable: retry in 30 s, don't stall the loop
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
                error = 'The server asks for a Plex token: put it on the second line of plex.txt'
            else:
                error = '%s: %s' % (self.server, getattr(e, 'reason', None) or e)
            state = {'state': 'error', 'error': error}
        if state.get('error') != self.error:
            self.error = state.get('error')
            if self.error:
                log('plex:', self.error)
        key = json.dumps(state, sort_keys=True)
        if key == self.sent and time.monotonic() - self.sent_at < 10:
            return
        reply = send('plex', state)
        self.sent, self.sent_at = key, time.monotonic()
        self.need = [str(k) for k in reply.get('need_art') or []]


# ------------------------------------------------------------------ TrueNAS

# A TrueNAS server on the local network: pools, disk temperatures, alerts and apps, through
# its JSON-RPC API over a WebSocket (wss://server/api/current). truenas.txt next to
# calendars.txt holds the address on the first line and an API key on the second. Always
# over TLS: TrueNAS revokes a key that is ever sent over plain http.
TRUENAS_FILE = os.path.join(os.path.dirname(CALENDAR_FILE), 'truenas.txt')
TRUENAS_CALLS = [
    ('system.info', []),
    ('pool.query', [[], {'select': ['name', 'status', 'healthy', 'size', 'allocated', 'free', 'scan']}]),
    ('disk.temperatures', [[]]),
    ('alert.list', []),
    ('app.query', [[], {'select': ['name', 'state', 'upgrade_available']}]),
]


class WebSocket:
    """Just enough of a WebSocket client for TrueNAS: text frames, masked as a client must."""

    def __init__(self, host, path, timeout=5):
        name, _, port = host.partition(':')
        tls = ssl.create_default_context()
        tls.check_hostname = False              # a TrueNAS certificate is usually self-signed;
        tls.verify_mode = ssl.CERT_NONE         # this is used for that one address only
        self.sock = tls.wrap_socket(socket.create_connection((name, int(port or 443)), timeout=timeout), server_hostname=name)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n' % (path, host, key)).encode())
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError('TrueNAS closed the connection')
            head += chunk
        head, self.buf = head.split(b'\r\n\r\n', 1)
        if b' 101 ' not in head.split(b'\r\n', 1)[0]:
            raise OSError('no WebSocket at %s%s: %s' % (host, path, head.split(b'\r\n', 1)[0].decode('latin-1')))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError('TrueNAS closed the connection')
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else
                                         bytes([0xFE]) + n.to_bytes(2, 'big') if n < 65536 else
                                         bytes([0xFF]) + n.to_bytes(8, 'big'))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text):
        self._frame(0x1, text.encode('utf-8'))

    def receive(self):
        message = b''
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = int.from_bytes(self._read(2), 'big')
            elif n == 127:
                n = int.from_bytes(self._read(8), 'big')
            payload = self._read(n)
            opcode = b0 & 0x0F
            if opcode == 0x8:
                raise OSError('TrueNAS closed the connection')
            if opcode == 0x9:
                self._frame(0xA, payload)      # pong
                continue
            if opcode in (0x0, 0x1, 0x2):
                message += payload
                if b0 & 0x80:
                    return message.decode('utf-8')

    def close(self):
        try:
            self._frame(0x8, b'')
            self.sock.close()
        except OSError:
            pass


class TrueNasLink:
    """Reads the server every 10 s while its page is on screen, else every 60 s (60 s after an
    error), and sends the Car Thing what changed, at least every 30 s."""

    def __init__(self):
        self.host = self.key = None
        self.next = 0.0
        self.sent, self.sent_at, self.error = None, 0.0, None

    def load(self):
        try:
            with open(TRUENAS_FILE) as f:
                lines = [l.strip() for l in f if l.strip() and not l.strip().startswith('#')]
        except OSError:
            lines = []
        host = re.sub(r'/.*$', '', re.sub(r'^[a-z]+://', '', lines[0])) if lines else None
        self.host, self.key = host, (lines[1] if len(lines) > 1 else None)

    def call_all(self):
        """Log in with the key and run TRUENAS_CALLS: {method: result or None}."""
        ws = WebSocket(self.host, '/api/current')
        try:
            results = {}
            for i, (method, params) in enumerate([('auth.login_with_api_key', [self.key])] + TRUENAS_CALLS):
                ws.send(json.dumps({'jsonrpc': '2.0', 'id': i + 1, 'method': method, 'params': params}))
                while True:          # skip notifications and other replies
                    reply = json.loads(ws.receive())
                    if reply.get('id') == i + 1:
                        break
                if i == 0:
                    if reply.get('result') is not True:
                        raise ValueError('TrueNAS refused the API key: check the second line of truenas.txt')
                    continue
                if 'error' in reply:
                    error = reply['error'] or {}
                    reason = (error.get('data') or {}).get('reason') or error.get('message')
                    if method == 'system.info':
                        raise ValueError('TrueNAS: %s' % reason)
                    log('truenas %s: %s' % (method, reason))
                results[method] = reply.get('result')
            return results
        finally:
            ws.close()

    def read(self):
        self.load()
        if not self.host:
            return {'state': 'none'}
        if not self.key:
            raise ValueError('No API key: put one on the second line of truenas.txt')
        r = self.call_all()
        info = r.get('system.info') or {}
        temps = r.get('disk.temperatures') or {}
        disks = [{'name': name, 'temp': t if isinstance(t, (int, float)) else (t or {}).get('temp')}
                 for name, t in sorted(temps.items())]
        pools = []
        for p in r.get('pool.query') or []:
            scan = p.get('scan') or {}
            pools.append({'name': p.get('name'), 'status': p.get('status'), 'healthy': bool(p.get('healthy')),
                          'size': p.get('size'), 'used': p.get('allocated'), 'free': p.get('free'),
                          'scan': {'kind': scan.get('function'), 'percent': scan.get('percentage')}
                          if scan.get('state') == 'SCANNING' else None})
        return {
            'state': 'ok',
            'system': {'host': info.get('hostname'), 'version': info.get('version'), 'uptime': int(info.get('uptime_seconds') or 0),
                       'memory': info.get('physmem'), 'cores': info.get('cores'), 'load': list(info.get('loadavg') or [])},
            'pools': pools,
            'disks': disks,
            'alerts': [{'level': a.get('level'), 'text': re.sub(r'<[^>]+>', '', a.get('formatted') or '').strip()}
                       for a in r.get('alert.list') or [] if not a.get('dismissed') and a.get('level') != 'INFO'],
            'apps': [{'name': a.get('name'), 'state': a.get('state'), 'update': bool(a.get('upgrade_available'))}
                     for a in r.get('app.query') or []],
        }

    def update(self):
        if time.monotonic() < self.next:
            return
        self.next = time.monotonic() + (10 if 'truenas' in visible else 60)
        try:
            state = self.read()
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self.next = time.monotonic() + 60           # server unreachable: retry in a minute, don't stall the loop
            message = str(e)
            if 'truenas.txt' not in message and not message.startswith('TrueNAS'):
                message = '%s: %s' % (self.host, message)
            state = {'state': 'error', 'error': message}
        if state.get('error') != self.error:
            self.error = state.get('error')
            if self.error:
                log('truenas:', self.error)
        key = json.dumps(state, sort_keys=True)
        if key == self.sent and time.monotonic() - self.sent_at < 30:
            return
        send('truenas', state)
        self.sent, self.sent_at = key, time.monotonic()


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
    """Time and UTC offset (they set the Car Thing's clock), the lock state and the agent's version."""
    offset = datetime.datetime.now().astimezone().utcoffset()
    return {'time_ms': int(time.time() * 1000), 'utc_offset_min': int(offset.total_seconds() // 60),
            'locked': locked, 'agent': {'os': 'unix', 'version': AGENT_VERSION}}


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
        try:
            print(json.dumps(PlexLink().read(), indent=2, ensure_ascii=False))
        except (OSError, ValueError, KeyError, TypeError) as e:
            print('plex:', e)
        try:
            print(json.dumps(TrueNasLink().read(), indent=2, ensure_ascii=False))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            print('truenas:', e)
        return
    log('started (adb: %s, port %d)' % (ADB, PORT))
    tunnel = False
    last_signature, last_sent = None, 0.0
    cover_sent_for, cover_tries = '', 0
    last_host, last_host_sent = None, 0.0
    last_stats, kill_result = 0.0, None
    audio = AudioLink()
    hue = HueLink()
    plex = PlexLink()
    truenas = TrueNasLink()
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
            # Panels that just came on screen: read their data now.
            if 'lights' in hue.opened:
                hue.fetched = 0.0
            if 'truenas' in hue.opened:
                truenas.next = 0.0
            if 'plex' in hue.opened:
                plex.next = 0.0
            if 'monitor' in hue.opened:
                stats.gpu_at = 0.0
            if 'github' in hue.opened:
                hue.github_refresh = True
            hue.opened.clear()
            if hue.github_refresh:
                github_fetched, hue.github_refresh = None, False
            if github_fetched is None or time.monotonic() - github_fetched >= (
                    300 if 'github' not in visible else 15 if github_busy else 60):
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
            plex.update()
            truenas.update()
        except (OSError, ValueError) as e:
            log('update failed:', e)
            tunnel = False
            time.sleep(2)
        time.sleep(1)


if __name__ == '__main__':
    main()
