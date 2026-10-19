#!/usr/bin/env python3
"""streamthing now-playing agent for Linux and macOS.

Reads what the computer is playing and pushes it to the Car Thing over USB
through `adb forward`:
  Linux  playerctl (any MPRIS player: Spotify, browsers, VLC, ...)
  macOS  nowplaying-cli; if that returns nothing (macOS 15.4+ restricts the API it
         uses), AppleScript for Spotify and Music

  python3 nowplaying.py          # run
  python3 nowplaying.py --once   # print one snapshot

install.sh sets it up to start at login. Environment: ADB (path to adb),
STREAMTHING_PORT (host side of the forward, default 28765).
"""
import base64
import hashlib
import json
import os
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


# ------------------------------------------------------------------ transport

def forward():
    return run([ADB, 'forward', 'tcp:%d' % PORT, 'tcp:8765'], timeout=10) is not None


def send(body):
    req = urllib.request.Request('http://127.0.0.1:%d/api/nowplaying' % PORT,
                                 data=json.dumps(body).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode('utf-8') or '{}')


def log(*args):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), *args, file=sys.stderr, flush=True)


def main():
    snapshot = mac_snapshot if sys.platform == 'darwin' else linux_snapshot
    if '--once' in sys.argv:
        np = snapshot()
        if np:
            np.pop('cover')
        print(json.dumps(np, indent=2, ensure_ascii=False) if np else 'nothing playing')
        return
    log('started (adb: %s, port %d)' % (ADB, PORT))
    tunnel = False
    last_signature, last_sent = None, 0.0
    cover_sent_for, cover_tries = '', 0
    while True:
        try:
            if not tunnel:
                # adb only runs at start and after a failed send (unplugged, rebooted).
                if not forward():
                    time.sleep(3)
                    continue
                tunnel = True
                last_signature, cover_sent_for = None, ''
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
                reply = send(body)
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
