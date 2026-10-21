#!/usr/bin/env python3
"""Retake the README screenshots on a Car Thing, with made-up demo data.

Stop the computer agent first, or it overwrites the demo data within seconds:
  macOS    launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.thingdeck.agent.plist
  Linux    systemctl --user stop thingdeck-agent
  Windows  Stop-ScheduledTask 'thingdeck agent'   (Start-ScheduledTask 'thingdeck agent' after)
then run this from the repo (adb on PATH, Pillow installed) and start the agent again:

  python3 docs/screenshots/take.py               # every screenshot
  python3 docs/screenshots/take.py media nas     # only these

It posts to the pad's API what the agent would send, drives the page through the kiosk's
DevTools port (tools/cdp.py), and saves 800×480 PNGs next to this file. The theme and accent
are whatever the pad is set to.
"""
import base64
import datetime
import hashlib
import io
import json
import math
import os
import random
import shlex
import subprocess
import sys
import threading
import time
import urllib.request

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PORT = 28765
ADB = os.environ.get('ADB', 'adb')
random.seed(7)


# ------------------------------------------------------------------ talking to the pad

def post(path, body, raw=None, content_type='application/json'):
    data = raw if raw is not None else json.dumps(body).encode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:%d/api/%s' % (PORT, path), data=data,
                                 headers={'Content-Type': content_type})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8') or '{}')


def adb(*args, **kw):
    return subprocess.run([ADB] + list(args), check=True, stdout=subprocess.PIPE, **kw).stdout


def js(code):
    adb('shell', 'python3 /var/macropad/tools/cdp.py eval %s' % shlex.quote(code))


def shot(name):
    adb('shell', 'python3 /var/macropad/tools/cdp.py screenshot /tmp/shot.png')
    out = os.path.join(HERE, name + '.png')
    adb('pull', '-q', '/tmp/shot.png', out)
    with Image.open(out) as im:
        if im.size != (800, 480):
            raise SystemExit('%s: unexpected size %s' % (name, im.size))
    print('saved', os.path.relpath(out, ROOT))


# ------------------------------------------------------------------ pictures

def png(im, fmt='JPEG'):
    buf = io.BytesIO()
    im.convert('RGB').save(buf, fmt, quality=88)
    return buf.getvalue()


def gradient(size, top, bottom):
    im = Image.new('RGB', size)
    d = ImageDraw.Draw(im)
    for y in range(size[1]):
        t = y / (size[1] - 1)
        d.line([(0, y), (size[0], y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    return im


def sunset(size=600):
    """A synthwave sunset over a grid: the cover of the demo song."""
    im = gradient((size, size), (38, 24, 74), (196, 82, 120))
    d = ImageDraw.Draw(im)
    horizon, r, cx = int(size * 0.68), int(size * 0.24), size // 2
    sun = gradient((2 * r, 2 * r), (255, 214, 102), (247, 99, 102))
    mask = Image.new('L', sun.size, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
    im.paste(sun, (cx - r, horizon - int(r * 1.45)), mask)
    for i in range(6):                                       # the bands across the sun
        y = horizon - int(r * 0.75) + i * int(r * 0.13)
        d.rectangle([cx - r, y, cx + r, y + 3 + i], fill=(60, 30, 92))
    d.rectangle([0, horizon, size, size], fill=(24, 14, 52))
    for i in range(-12, 13):
        d.line([(cx + i * size // 24, horizon), (cx + i * size // 3, size)], fill=(112, 214, 255), width=2)
    y, step = horizon, 6
    while y < size:
        d.line([(0, y), (size, y)], fill=(112, 214, 255), width=2)
        step *= 1.45
        y += int(step)
    return im


def poster(kind):
    im = {'orbit': gradient((240, 360), (28, 30, 70), (214, 112, 84)),
          'summit': gradient((240, 360), (40, 92, 110), (230, 200, 120))}.get(kind) or sunset(360)
    d = ImageDraw.Draw(im)
    if kind == 'orbit':
        d.ellipse([60, 120, 180, 240], fill=(236, 226, 214))
        d.ellipse([30, 168, 210, 192], outline=(250, 240, 230), width=5)
    elif kind == 'summit':
        d.polygon([(120, 90), (40, 270), (200, 270)], fill=(245, 246, 240))
        d.rectangle([0, 270, 240, 360], fill=(54, 66, 40))
    return png(im)


# ------------------------------------------------------------------ demo data

def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def ics_time(dt):
    return dt.astimezone(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def calendar(reminder=False):
    """A week of events around now in two calendars, Work and Personal; with reminder, one that starts in 3:16."""
    now = datetime.datetime.now().astimezone()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    at = lambda days, h, m: day + datetime.timedelta(days=days, hours=h, minutes=m)
    start = now - datetime.timedelta(minutes=10)
    events = [
        ('Design sync', start, start + datetime.timedelta(minutes=65), 'Room 4B', 'https://meet.google.com/abc-defg-hij', ''),
        ('Daily standup', at(1, 9, 30), at(1, 9, 45), '', 'https://meet.google.com/xyz-abcd-efg', 'FREQ=DAILY;COUNT=4'),
        ('Lunch with Ana', at(1, 13, 0), at(1, 14, 0), 'Café Central', '', ''),
        ('Code review: audio picker', at(1, 16, 0), at(1, 16, 45), '',
         'Walk through the device picker and the volume bar.\\nJoin: https://zoom.us/j/1234567890', ''),
        ('Team offsite', None, None, 'Mountain cabin', '', ''),
        ('Dentist', at(2, 18, 0), at(2, 18, 30), '', '', ''),
    ]
    if reminder:
        soon = now + datetime.timedelta(minutes=3, seconds=16)
        events.append(('Design review', soon, soon + datetime.timedelta(minutes=30), 'Room 4B',
                       'https://meet.google.com/rev-iewd-sgn', ''))
    personal = ('Lunch with Ana', 'Team offsite', 'Dentist')
    lines = []
    for name in ('Work', 'Personal'):         # the agent sends the calendars one after the other
        lines += ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//thingdeck//demo//EN', 'X-WR-CALNAME:' + name]
        for i, (title, begin, end, place, notes, rule) in enumerate(events):
            if (title in personal) != (name == 'Personal'):
                continue
            lines += ['BEGIN:VEVENT', 'UID:demo-%d@thingdeck' % i, 'SUMMARY:' + title]
            if begin is None:      # all day, the day after tomorrow
                lines += ['DTSTART;VALUE=DATE:' + (day + datetime.timedelta(days=2)).strftime('%Y%m%d'),
                          'DTEND;VALUE=DATE:' + (day + datetime.timedelta(days=3)).strftime('%Y%m%d')]
            else:
                lines += ['DTSTART:' + ics_time(begin), 'DTEND:' + ics_time(end)]
            lines += (['LOCATION:' + place] if place else []) + (['DESCRIPTION:' + notes] if notes else []) + \
                     (['RRULE:' + rule] if rule else []) + ['END:VEVENT']
        lines.append('END:VCALENDAR')
    return ('\r\n'.join(lines) + '\r\n').encode('utf-8')


COVER = png(sunset())
SONG = {'title': 'Neon Skyline', 'artist': 'The Midnight Sample', 'album': 'Demo Tapes', 'app': 'Spotify',
        'playing': True, 'position_ms': 99000, 'duration_ms': 241000, 'shuffle': False, 'repeat': None}
SONG['art_key'] = hashlib.md5(b'neon-skyline').hexdigest()

AUDIO = {'volume': 42, 'muted': False, 'output': 'spk', 'input': 'mic',
         'outputs': [{'id': 'spk', 'name': 'Speakers (USB Audio Interface)'},
                     {'id': 'hp', 'name': 'Headphones (Wireless Headset)'},
                     {'id': 'hdmi', 'name': 'Monitor (HDMI Audio)'}],
         'inputs': [{'id': 'mic', 'name': 'Microphone (USB Audio Interface)'},
                    {'id': 'hsmic', 'name': 'Headset Microphone (Wireless Headset)'}]}


def stats_sample(i):
    return {'cpu': round(34 + 9 * math.sin(i / 3.1) + random.uniform(-6, 6), 1), 'cpu_temp': 54,
            'mem_total_mb': 32768, 'mem_used_mb': 13900 + int(150 * math.sin(i / 9)),
            'gpu': max(0, int(30 + 28 * math.sin(i / 5.0))), 'gpu_temp': 51,
            'gpu_mem_used_mb': 3380, 'gpu_mem_total_mb': 12288,
            'net_rx': int(max(0, 500000 + 420000 * math.sin(i / 1.7) + random.uniform(-40000, 40000))),
            'net_tx': int(max(0, 90000 + 50000 * math.sin(i / 2.3))),
            'focus': {'pid': 4242, 'name': 'Electron', 'app': 'Visual Studio Code',
                      'title': 'macropad.py — thingdeck', 'protected': False}}


def light(lid, name, on, bri, xy=None, mirek=None):
    return {'id': lid, 'name': name, 'on': on, 'brightness': bri, 'xy': xy, 'mirek': mirek, 'ct': True}


def room(rid, kind, name, lights, scenes=(), active=None, archetype='living_room'):
    on = [l for l in lights if l['on']]
    return {'id': rid, 'kind': kind, 'name': name, 'archetype': archetype, 'group': rid + '-g', 'on': bool(on),
            'brightness': max([l['brightness'] for l in on] or [0]) or None, 'lights': lights,
            'scenes': [{'id': '%s-%d' % (rid, i), 'name': s, 'active': s == active} for i, s in enumerate(scenes)]}


WARM, PURPLE, BLUE = [0.52, 0.41], [0.27, 0.12], [0.17, 0.08]
SCENES = ('Relax', 'Read', 'Concentrate', 'Energize', 'Nightlight', 'Sunset')
HUE = {'state': 'ok', 'rooms': [
    room('living', 'room', 'Living room', [light('l1', 'Ceiling', True, 70, WARM, 400), light('l2', 'Floor lamp', True, 70, WARM, 400),
                                           light('l3', 'TV strip', True, 70, WARM, 400)], SCENES, 'Relax'),
    room('kitchen', 'room', 'Kitchen', [light('k1', 'Spots', False, 80, None, 300), light('k2', 'Island', False, 80, None, 300)],
         SCENES[:4], archetype='kitchen'),
    room('bedroom', 'room', 'Bedroom', [light('b1', 'Bedside', True, 30, PURPLE), light('b2', 'Ceiling', False, 50, None, 370)],
         SCENES, archetype='bedroom'),
    room('office', 'room', 'Office', [light('o1', 'Desk lamp', True, 100, None, 233)], SCENES, 'Concentrate', 'office'),
    room('hallway', 'room', 'Hallway', [light('h1', 'Ceiling', False, 40, None, 370)], archetype='hallway'),
    room('desk', 'zone', 'Desk', [light('d1', 'Light bar', True, 60, BLUE), light('d2', 'Monitor glow', True, 60, BLUE)], SCENES[:3]),
    room('downstairs', 'zone', 'Downstairs', [light('l1', 'Ceiling', True, 70, WARM, 400), light('k1', 'Spots', False, 80, None, 300)]),
]}

POSTERS = {'orbit': poster('orbit'), 'summit': poster('summit'), 'neon': poster('neon')}


def plex(with_posters):
    wave = lambda base, amp, n, k: [round(max(0, base + amp * math.sin(i / k) + random.uniform(-amp / 3, amp / 3)), 1) for i in range(n)]
    state = {
        'state': 'ok',
        'sessions': [
            {'id': 's1', 'kind': 'movie', 'title': 'Orbit', 'sub': '2024 · Sci-fi', 'user': 'alex', 'player': 'Living room TV',
             'state': 'playing', 'position_ms': 2900000, 'duration_ms': 7200000, 'decision': 'direct play', 'hw': False,
             'quality': '4K', 'kbps': 38500, 'local': True, 'art': 'orbit'},
            {'id': 's2', 'kind': 'episode', 'title': 'The Summit', 'sub': 'Ridgeline · S2 · E5', 'user': 'sam', 'player': 'Phone',
             'state': 'playing', 'position_ms': 1300000, 'duration_ms': 2700000, 'decision': 'transcode', 'hw': True,
             'quality': '1080p', 'kbps': 4200, 'local': False, 'art': 'summit'},
        ],
        'load': {'cpu': wave(24, 6, 60, 4), 'mem': wave(45, 2, 60, 9), 'plex_cpu': 14.2, 'plex_mem': 6.1},
        'net': {'lan': [int(x * 100000) for x in wave(36, 9, 60, 3)], 'wan': [int(x * 10000) for x in wave(34, 6, 60, 5)]},
        'libraries': [{'title': 'Movies', 'kind': 'movie', 'count': 412},
                      {'title': 'TV Shows', 'kind': 'show', 'count': 38, 'episodes': 1694},
                      {'title': 'Music', 'kind': 'artist', 'count': 120, 'albums': 236, 'tracks': 2810}],
        'activities': [{'title': 'Detecting intros', 'sub': 'Ridgeline · 14 of 40 episodes', 'progress': 35}],
        'server': {'name': 'home-server', 'version': '1.41.3.9314', 'update': None, 'transcodes': 1},
    }
    if with_posters:
        state['posters'] = {k: base64.b64encode(v).decode('ascii') for k, v in POSTERS.items()}
    return state


TB, GB = 1024 ** 4, 1024 ** 3
WEATHER = {'state': 'ok', 'place': 'Paris', 'temp': 17.4, 'code': 2, 'day': True, 'unit': 'C'}

TRUENAS = {
    'state': 'ok',
    'system': {'host': 'nas', 'version': '25.10.7', 'uptime': 14 * 86400 + 6 * 3600, 'memory': 64 * GB, 'cores': 8,
               'load': [0.42, 0.38, 0.35]},
    'pools': [{'name': 'tank', 'status': 'ONLINE', 'healthy': True, 'size': 24 * TB, 'used': int(14.4 * TB), 'free': int(9.6 * TB),
               'scan': {'kind': 'SCRUB', 'percent': 42.0}},
              {'name': 'fast', 'status': 'ONLINE', 'healthy': True, 'size': 1830 * GB, 'used': 1574 * GB, 'free': 256 * GB, 'scan': None},
              {'name': 'backup', 'status': 'DEGRADED', 'healthy': False, 'size': int(7.2 * TB), 'used': int(3.1 * TB),
               'free': int(4.1 * TB), 'scan': None}],
    'disks': [{'name': 'sda', 'temp': 34}, {'name': 'sdb', 'temp': 36}, {'name': 'sdc', 'temp': 47}, {'name': 'sdd', 'temp': 35},
              {'name': 'nvme0n1', 'temp': 41}],
    'alerts': [{'level': 'WARNING', 'text': 'Pool backup state is DEGRADED: One or more devices has been removed.'}],
    'apps': [{'name': 'plex', 'state': 'RUNNING', 'update': False}, {'name': 'nextcloud', 'state': 'RUNNING', 'update': True},
             {'name': 'immich', 'state': 'RUNNING', 'update': False}, {'name': 'syncthing', 'state': 'STOPPED', 'update': False}],
    'jobs': [{'method': 'pool.scrub.scrub', 'target': 'tank', 'percent': 42.0, 'text': None, 'started': None},
             {'method': 'cloudsync.sync', 'target': None, 'percent': 71.0, 'text': 'Sending photos', 'started': None}],
}


def github():
    now = datetime.datetime.now().astimezone()
    ago = lambda minutes: iso(now - datetime.timedelta(minutes=minutes))
    run = lambda wf, status, concl, branch, title, minutes, event='push': {
        'workflow': wf, 'status': status, 'conclusion': concl, 'branch': branch, 'title': title, 'event': event,
        'created': ago(minutes), 'updated': ago(minutes), 'url': 'https://github.com/your-name/your-project/actions'}
    pr = lambda repo, n, title, minutes, review, checks, draft=False: {
        'repo': repo, 'number': n, 'title': title, 'draft': draft, 'review': review, 'updated': ago(minutes),
        'url': 'https://github.com/%s/pull/%d' % (repo, n), 'checks': checks}
    return {'repo': 'your-name/your-project',
            'runs': [run('CI', 'in_progress', '', 'main', 'Add the audio device picker', 2),
                     run('Deploy preview', 'completed', 'success', 'feature/calendar', 'Calendar: tap an event for details', 31),
                     run('Lint', 'completed', 'failure', 'fix/docs', 'Fix typos in the README', 120),
                     run('Nightly e2e', 'queued', '', 'main', 'Scheduled run', 0, 'schedule'),
                     run('Release', 'completed', 'success', 'main', 'v2.3.0', 2880)],
            'prs': [pr('your-name/your-project', 128, 'Add the audio device picker to the Media page', 3, 'REVIEW_REQUIRED', 'PENDING'),
                    pr('your-name/your-project', 121, 'Calendar: tap an event for details', 45, 'APPROVED', 'SUCCESS'),
                    pr('your-name/other-lib', 7, 'Support Python 3.13', 1440, None, 'FAILURE', True)]}


# ------------------------------------------------------------------ feeding the pad

class Feeder:
    """Sends what the agent would, every second, so nothing on the pad expires mid-shoot."""

    def __init__(self):
        self.locked, self.i, self.stop = False, 0, threading.Event()
        utc_offset = datetime.datetime.now().astimezone().utcoffset().total_seconds() // 60
        version = hashlib.sha1(open(os.path.join(ROOT, 'host', 'unix', 'nowplaying.py'), 'rb').read()).hexdigest()[:12]
        self.host = lambda: {'time_ms': int(time.time() * 1000), 'utc_offset_min': int(utc_offset), 'locked': self.locked,
                             'agent': {'os': 'unix', 'version': version}}

    def once(self):
        post('host', self.host())
        self.i += 1
        post('stats', stats_sample(self.i))
        post('audio', AUDIO)
        if post('nowplaying', SONG).get('need_art'):
            post('nowplaying', dict(SONG, art=base64.b64encode(COVER).decode('ascii'), art_type='image/jpeg'))
        if self.i % 5 == 1:
            post('hue', HUE)
            post('truenas', TRUENAS)
            post('weather', WEATHER)
            post('github', github())
            post('plex', plex(with_posters=True))

    def loop(self):
        while not self.stop.wait(1):
            try:
                self.once()
            except OSError as e:
                print('feed:', e)

    def start(self):
        for _ in range(60):                 # fill the Status graphs (one point per update)
            self.i += 1
            post('stats', stats_sample(self.i))
        self.once()
        post('calendar', None, raw=calendar(), content_type='text/calendar; charset=utf-8')
        threading.Thread(target=self.loop, daemon=True).start()


# ------------------------------------------------------------------ the shots

def page(name):
    post('page', {'page': name})


def settings(section):
    js("settingsSec = '%s'; openSettings(true);" % section)


def scene(feeder, name):
    """Put the screen in the state for one screenshot; returns how to undo it."""
    close = lambda: js('if (sheetOpen) closeSheet(); if (showSettings) openSettings(false);')
    if name in ('media', 'calendar', 'status', 'code', 'lights', 'plex', 'nas'):
        page({'media': 'Media', 'calendar': 'Calendar', 'status': 'Status', 'code': 'Code', 'lights': 'Lights',
              'plex': 'Plex', 'nas': 'NAS'}[name])
        return None
    if name == 'event':
        page('Calendar')
        time.sleep(1)
        js("openEvent(agenda.events.filter(function (e) { return e.title === 'Code review: audio picker'; })[0].id);")
        return close
    if name == 'audio':
        page('Media')
        time.sleep(1)
        js('openAudio();')
        return close
    if name == 'volume':
        page('Media')
        time.sleep(1)
        js('showVolume(); clearTimeout(volTimer);')
        return lambda: js("$('volhud').classList.remove('show');")
    if name == 'room':
        page('Lights')
        time.sleep(1)
        js("openRoom('living');")
        return close
    if name in ('settings', 'tabs', 'general'):
        settings({'settings': 'appearance'}.get(name, name))
        return close
    if name == 'lock':
        feeder.locked = True
        post('host', feeder.host())

        def unlock():
            feeder.locked = False
            post('host', feeder.host())
        return unlock
    if name == 'standby':
        js("st.usb = 'suspended'; updateStandby();")
        time.sleep(3.5)                        # the standby screen waits 3 s before it shows
        return lambda: js("st.usb = 'connected'; updateStandby();")
    if name == 'reminder':
        page('Media')
        post('calendar', None, raw=calendar(reminder=True), content_type='text/calendar; charset=utf-8')
        time.sleep(1)
        js('checkReminder();')
        return lambda: post('calendar', None, raw=calendar(), content_type='text/calendar; charset=utf-8')
    raise SystemExit('unknown screenshot: ' + name)


SHOTS = ['media', 'calendar', 'status', 'code', 'event', 'audio', 'volume', 'lock', 'lights', 'room', 'plex', 'nas',
         'settings', 'tabs', 'general', 'standby', 'reminder']


def main():
    names = sys.argv[1:] or SHOTS
    adb('forward', 'tcp:%d' % PORT, 'tcp:8765')
    feeder = Feeder()
    feeder.start()
    time.sleep(3)                               # covers and posters load
    try:
        for name in names:
            undo = scene(feeder, name)
            time.sleep(1.5)                     # let transitions finish
            shot(name)
            if undo:
                undo()
            time.sleep(0.5)
    finally:
        feeder.stop.set()
        page('Media')


if __name__ == '__main__':
    main()
