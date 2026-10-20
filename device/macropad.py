#!/usr/bin/env python3
"""Car Thing macro pad.

Shows up on the host as a USB HID keyboard + media keys + mouse wheel (built in
user space on FunctionFS, because this kernel has no f_hid). The dial, the
hardware buttons and a touch UI in the kiosk browser send the reports.
"""
import base64
import collections
import datetime
import fcntl
import glob
import json
import math
import os
import queue
import select
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ics       # ics.py next to this file: the calendar reader for the agenda

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'config.json')
STATE_PATH = os.path.join(BASE, 'state.json')
GADGET_SH = os.path.join(BASE, 'gadget.sh')
FFS_DIR = '/dev/usb-ffs/hid'
WEBAPP_DIR = '/usr/share/qt-superbird-app/webapp'
BACKLIGHT = '/sys/class/backlight/aml-bl/brightness'
BRIGHTNESS_MIN = 5          # brightness levels run 5..255 (Settings slider)
BRIGHTNESS_STOPS = 3        # auto brightness offset: -3..+3 stops in half stops (a stop doubles or halves the light)
LIGHT_SENSOR = '/sys/bus/iio/devices/iio:device*/in_intensity0_raw'    # the TMD2772 ambient light sensor
THEMES = ('dark', 'black', 'light', 'nord', 'dracula', 'catppuccin', 'gruvbox', 'tokyonight', 'solarized',
          'onedark', 'monokai')
ACCENTS = ('theme', 'green', 'blue', 'purple', 'orange', 'pink', 'red')   # palettes: web/index.html; or a hue 0-359
OS_MODES = ('auto', 'windows', 'mac', 'linux')
ICON_STYLES = ('solid', 'outline', 'emoji')   # icon sets: web/index.html
ICON_COLORS = ('neutral', 'accent', 'category')
HTTP_PORT = 8765


def log(*args):
    print(time.strftime('%H:%M:%S'), *args, flush=True)


def page_key(entry):
    """What the tab order calls a config.json page entry: its name."""
    return entry.get('name')


# ---------------------------------------------------------------- HID tables

REPORT_DESCRIPTOR = bytes([
    # Report 1: keyboard (modifiers, reserved, 6 keys)
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x05, 0x07, 0x19, 0x00, 0x2A, 0xFF, 0x00, 0x81, 0x00,
    0xC0,
    # Report 2: consumer control (one 16-bit usage)
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x02,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
    0xC0,
    # Report 3: mouse (3 buttons, x, y, wheel, horizontal pan)
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x03, 0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x03, 0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x05, 0x81, 0x03,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03, 0x81, 0x06,
    0x05, 0x0C, 0x0A, 0x38, 0x02, 0x95, 0x01, 0x81, 0x06,
    0xC0, 0xC0,
    # Report 4: telephony phone mute (Linux maps it to KEY_MICMUTE)
    0x05, 0x0B, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x04,
    0x09, 0x2F, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x01, 0x81, 0x02,
    0x75, 0x07, 0x95, 0x01, 0x81, 0x03,
    0xC0,
])

MODIFIERS = {
    'ctrl': 0x01, 'control': 0x01, 'shift': 0x02, 'alt': 0x04, 'option': 0x04,
    'win': 0x08, 'gui': 0x08, 'cmd': 0x08, 'meta': 0x08, 'super': 0x08,
    'rctrl': 0x10, 'rshift': 0x20, 'ralt': 0x40, 'altgr': 0x40, 'rwin': 0x80,
}

KEYS = {
    'enter': 0x28, 'return': 0x28, 'esc': 0x29, 'escape': 0x29, 'backspace': 0x2A, 'tab': 0x2B,
    'space': 0x2C, 'minus': 0x2D, 'equal': 0x2E, 'capslock': 0x39,
    'printscreen': 0x46, 'prtsc': 0x46, 'scrolllock': 0x47, 'pause': 0x48, 'insert': 0x49,
    'home': 0x4A, 'pageup': 0x4B, 'pgup': 0x4B, 'delete': 0x4C, 'del': 0x4C, 'end': 0x4D,
    'pagedown': 0x4E, 'pgdn': 0x4E, 'right': 0x4F, 'left': 0x50, 'down': 0x51, 'up': 0x52,
    'numlock': 0x53, 'menu': 0x65, 'app': 0x65,
}
for _i in range(26):
    KEYS[chr(ord('a') + _i)] = 0x04 + _i
for _i, _c in enumerate('1234567890'):
    KEYS[_c] = 0x1E + _i
for _i in range(12):
    KEYS['f%d' % (_i + 1)] = 0x3A + _i
    KEYS['f%d' % (_i + 13)] = 0x68 + _i

# Host keyboard layouts: char -> list of strokes (usage, modifiers). HID sends key
# positions, so text has to be translated for the layout the host is set to.
SHIFT, ALTGR = 0x02, 0x40


def _base_layout():
    layout = {'\n': [(0x28, 0)], '\t': [(0x2B, 0)], ' ': [(0x2C, 0)]}
    for c in 'abcdefghijklmnopqrstuvwxyz':
        layout[c] = [(KEYS[c], 0)]
        layout[c.upper()] = [(KEYS[c], SHIFT)]
    for c in '1234567890':
        layout[c] = [(KEYS[c], 0)]
    return layout


def _layout_us():
    layout = _base_layout()
    for plain, shifted, usage in zip('1234567890-=[]\\;\'`,./', '!@#$%^&*()_+{}|:"~<>?',
                                     [0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27,
                                      0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38]):
        if plain not in layout:
            layout[plain] = [(usage, 0)]
        layout[shifted] = [(usage, SHIFT)]
    return layout


def _layout_latam():
    """Latin American: the Windows 'Latin American' and xkb 'latam' layouts, which match.
    macOS's 'Latin American' layout puts symbols elsewhere (Option, not AltGr); use 'us' there."""
    layout = _base_layout()
    space = (0x2C, 0)
    keys = {
        '|': (0x35, 0), '°': (0x35, SHIFT), '¬': (0x35, ALTGR),
        '!': (0x1E, SHIFT), '"': (0x1F, SHIFT), '#': (0x20, SHIFT), '$': (0x21, SHIFT), '%': (0x22, SHIFT),
        '&': (0x23, SHIFT), '/': (0x24, SHIFT), '(': (0x25, SHIFT), ')': (0x26, SHIFT), '=': (0x27, SHIFT),
        "'": (0x2D, 0), '?': (0x2D, SHIFT), '\\': (0x2D, ALTGR), '¿': (0x2E, 0), '¡': (0x2E, SHIFT),
        '@': (0x14, ALTGR), '+': (0x30, 0), '*': (0x30, SHIFT), 'ñ': (0x33, 0), 'Ñ': (0x33, SHIFT),
        '{': (0x34, 0), '[': (0x34, SHIFT), '}': (0x31, 0), ']': (0x31, SHIFT), '<': (0x64, 0), '>': (0x64, SHIFT),
        ',': (0x36, 0), ';': (0x36, SHIFT), '.': (0x37, 0), ':': (0x37, SHIFT), '-': (0x38, 0), '_': (0x38, SHIFT),
    }
    for c, stroke in keys.items():
        layout[c] = [stroke]
    # Dead keys: press, then space to get the bare character.
    layout['~'] = [(0x30, ALTGR), space]
    layout['^'] = [(0x34, ALTGR), space]
    layout['`'] = [(0x31, ALTGR), space]
    layout['´'] = [(0x2F, 0), space]
    for plain, accented in zip('aeiou', 'áéíóú'):
        layout[accented] = [(0x2F, 0), (KEYS[plain], 0)]
        layout[accented.upper()] = [(0x2F, 0), (KEYS[plain], SHIFT)]
    layout['ü'] = [(0x2F, SHIFT), (KEYS['u'], 0)]
    layout['Ü'] = [(0x2F, SHIFT), (KEYS['u'], SHIFT)]
    return layout


LAYOUTS = {'us': _layout_us(), 'latam': _layout_latam()}
layout = LAYOUTS['us']   # active layout, set from config

MEDIA = {
    'play_pause': 0xCD, 'next': 0xB5, 'prev': 0xB6, 'previous': 0xB6, 'stop': 0xB7,
    'mute': 0xE2, 'vol_up': 0xE9, 'vol_down': 0xEA, 'calculator': 0x192, 'explorer': 0x194,
    'mail': 0x18A, 'browser': 0x196, 'browser_back': 0x224, 'browser_forward': 0x225,
    'browser_refresh': 0x227, 'search': 0x221, 'brightness_up': 0x6F, 'brightness_down': 0x70,
}

MOUSE_BUTTONS = {'left': 1, 'right': 2, 'middle': 4}


def parse_combo(combo):
    """'ctrl+shift+p' -> (modifier byte, [usages]). Single characters ('ctrl+ñ',
    'win+.') are looked up in the active layout; use 'plus' for the + key."""
    mods, keys = 0, []
    for part in combo.split('+'):
        part = part.strip()
        name = part.lower()
        if not part:
            continue
        if name == 'plus':
            part = name = '+'
        if name in MODIFIERS:
            mods |= MODIFIERS[name]
        elif name in KEYS:
            keys.append(KEYS[name])
        elif len(part) == 1 and part in layout and len(layout[part]) == 1:
            usage, extra = layout[part][0]
            keys.append(usage)
            mods |= extra
        else:
            raise ValueError('unknown key %r in %r' % (part, combo))
    return mods, keys[:6]


# --------------------------------------------------------- FunctionFS HID

FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3
FUNCTIONFS_STRINGS_MAGIC = 2
FFS_HAS_FS_DESC, FFS_HAS_HS_DESC = 1, 2
EV_BIND, EV_UNBIND, EV_ENABLE, EV_DISABLE, EV_SETUP, EV_SUSPEND, EV_RESUME = range(7)


def ffs_descriptors():
    hid = struct.pack('<BBHBBBH', 9, 0x21, 0x0111, 0, 1, 0x22, len(REPORT_DESCRIPTOR))
    intf = struct.pack('<BBBBBBBBB', 9, 4, 0, 0, 1, 3, 0, 0, 1)

    def ep(interval):
        return struct.pack('<BBBBHB', 7, 5, 0x81, 3, 16, interval)

    fs = intf + hid + ep(1)   # 1 ms
    hs = intf + hid + ep(4)   # 2^(4-1) * 125 us = 1 ms
    body = struct.pack('<II', 3, 3) + fs + hs
    head = struct.pack('<III', FUNCTIONFS_DESCRIPTORS_MAGIC_V2, 12 + len(body),
                       FFS_HAS_FS_DESC | FFS_HAS_HS_DESC)
    return head + body, hid


def ffs_strings():
    body = struct.pack('<IIH', 1, 1, 0x0409) + b'Car Thing Macro Pad\0'
    return struct.pack('<II', FUNCTIONFS_STRINGS_MAGIC, 8 + len(body)) + body


class Hid:
    def __init__(self, on_status):
        self.on_status = on_status
        self.on_report_request = None   # called with wLength of GET_DESCRIPTOR(report)
        self.status = 'off'
        self.ep0 = None
        self.ep1 = None
        self.q = queue.Queue(maxsize=256)
        self.hid_desc = b''

    # -- lifecycle
    def open(self):
        descs, self.hid_desc = ffs_descriptors()
        self.ep0 = os.open(os.path.join(FFS_DIR, 'ep0'), os.O_RDWR)
        os.write(self.ep0, descs)
        os.write(self.ep0, ffs_strings())
        threading.Thread(target=self._ep0_loop, name='ep0', daemon=True).start()
        threading.Thread(target=self._writer, name='hid-writer', daemon=True).start()
        self._set_status('disconnected')

    def close(self):
        for fd in (self.ep1, self.ep0):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.ep0 = self.ep1 = None

    def _set_status(self, status):
        if status != self.status:
            self.status = status
            log('usb:', status)
            self.on_status(status)

    def _ep0_loop(self):
        while self.ep0 is not None:
            try:
                buf = os.read(self.ep0, 12 * 4)
            except OSError as e:
                log('ep0 read failed:', e)
                time.sleep(0.5)
                continue
            for off in range(0, len(buf) - 11, 12):
                setup = buf[off:off + 8]
                kind = buf[off + 8]
                if kind == EV_SETUP:
                    self._setup(*struct.unpack('<BBHHH', setup))
                elif kind == EV_ENABLE or kind == EV_RESUME:
                    self._set_status('connected')
                elif kind == EV_SUSPEND:
                    self._set_status('suspended')
                elif kind in (EV_DISABLE, EV_UNBIND):
                    self._set_status('disconnected')

    def _setup(self, req_type, req, value, index, length):
        try:
            if req_type == 0x81 and req == 0x06:              # GET_DESCRIPTOR (interface)
                dtype = value >> 8
                if dtype == 0x22:
                    os.write(self.ep0, REPORT_DESCRIPTOR[:length])
                    if self.on_report_request:
                        self.on_report_request(length)
                    return
                if dtype == 0x21:
                    os.write(self.ep0, self.hid_desc[:length])
                    return
            elif req_type == 0x21 and req in (0x0A, 0x0B, 0x09):  # SET_IDLE / SET_PROTOCOL / SET_REPORT
                os.read(self.ep0, length)
                return
            elif req_type == 0xA1 and req == 0x01:            # GET_REPORT
                rid = value & 0xFF
                size = {1: 9, 2: 3, 3: 6, 4: 2}.get(rid, 9)
                os.write(self.ep0, (bytes([rid]) + bytes(size - 1))[:length])
                return
            elif req_type == 0xA1 and req == 0x02:            # GET_IDLE
                os.write(self.ep0, b'\x00'[:length])
                return
            elif req_type == 0xA1 and req == 0x03:            # GET_PROTOCOL
                os.write(self.ep0, b'\x01'[:length])
                return
            log('stall setup %02x %02x %04x %04x %d' % (req_type, req, value, index, length))
            # Stall: transfer in the wrong direction.
            if req_type & 0x80:
                os.read(self.ep0, 0)
            else:
                os.write(self.ep0, b'')
        except OSError:
            pass

    def _writer(self):
        while True:
            report, pause = self.q.get()
            if self.status != 'connected':
                continue
            try:
                if self.ep1 is None:
                    self.ep1 = os.open(os.path.join(FFS_DIR, 'ep1'), os.O_RDWR)
                os.write(self.ep1, report)
            except OSError as e:
                log('ep1 write failed:', e)
                try:
                    os.close(self.ep1)
                except (OSError, TypeError):
                    pass
                self.ep1 = None
            if pause:
                time.sleep(pause)

    # -- reports
    def _send(self, report, pause=0.0):
        if self.status != 'connected':
            return
        try:
            self.q.put_nowait((bytes(report), pause))
        except queue.Full:
            pass

    def keyboard(self, mods, keys, pause=0.0):
        keys = list(keys)[:6]
        self._send([1, mods, 0] + keys + [0] * (6 - len(keys)), pause)

    def combo(self, mods, keys, hold=0.02):
        if mods and keys:
            self.keyboard(mods, [], 0.01)
        self.keyboard(mods, keys, hold)
        if mods and keys:
            self.keyboard(mods, [], 0.01)
        self.keyboard(0, [])

    def type_text(self, text):
        for ch in text:
            for usage, mods in layout.get(ch, ()):
                if mods:
                    self.keyboard(mods, [], 0.004)
                self.keyboard(mods, [usage], 0.006)
                self.keyboard(0, [], 0.004)

    def consumer(self, usage):
        self._send([2, usage & 0xFF, usage >> 8], 0.02)
        self._send([2, 0, 0])

    def mic_mute(self):
        self._send([4, 1], 0.02)
        self._send([4, 0])

    def mouse(self, buttons=0, wheel=0, pan=0):
        self._send([3, buttons, 0, 0, wheel & 0xFF, pan & 0xFF])

    def click(self, button):
        self.mouse(buttons=button)
        self.mouse()


# ------------------------------------------------------------- UI events

HOST_TTL = 30           # seconds without a heartbeat before the host agent counts as gone
VALID_TIME = 1735689600  # 2025-01-01: earlier means the clock was never set (the device has no RTC)


class HostLink:
    """Heartbeat from the host agent (host/ in the repo): the computer's time and UTC
    offset, which set this device's clock, and whether the computer's screen is locked."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.utc_offset = None     # seconds east of UTC, from the host (follows DST)
        self.locked = False
        self.seen = None           # monotonic time of the last heartbeat
        self.agent = None          # {os, version}: the agent's own script fingerprint

    def update(self, body):
        changed = False
        host_time = body.get('time_ms')
        if isinstance(host_time, (int, float)) and host_time / 1000 > VALID_TIME:
            if abs(time.time() - host_time / 1000) > 2:
                was_valid = time.time() > VALID_TIME
                try:
                    time.clock_settime(time.CLOCK_REALTIME, host_time / 1000)
                    log('clock set from the computer: %s UTC' % time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()))
                except OSError as e:
                    log('could not set the clock:', e)
                changed = changed or not was_valid
        offset = body.get('utc_offset_min')
        if isinstance(offset, int) and offset * 60 != self.utc_offset:
            self.utc_offset = offset * 60
            changed = True
        agent = body.get('agent')
        # An agent older than the version check sends none: keep a version of None, so the
        # page says it is out of date instead of waiting for data that agent never sends.
        agent = ({'os': str(agent.get('os')), 'version': str(agent.get('version'))} if isinstance(agent, dict)
                 else {'os': None, 'version': None})
        if agent != self.agent:
            self.agent = agent
            changed = True
        locked = bool(body.get('locked'))
        if locked != self.locked:
            self.locked = locked
            log('computer %s' % ('locked' if locked else 'unlocked'))
            changed = True
        self.seen = time.monotonic()
        if changed:
            self.on_change()

    def expire(self):
        """Agent gone (stopped, computer off): drop the lock state; the clock keeps running."""
        if self.locked and self.seen is not None and time.monotonic() - self.seen > HOST_TTL:
            self.locked = False
            self.on_change()

    def clock(self):
        if self.utc_offset is None or time.time() < VALID_TIME:
            return None
        return {'utc_offset': self.utc_offset}


NOWPLAYING_TTL = 15     # seconds without an update before the host agent counts as gone
NOWPLAYING_FIELDS = ('title', 'artist', 'album', 'album_artist', 'track_number', 'app', 'playing',
                     'position_ms', 'duration_ms', 'shuffle', 'repeat')


class NowPlaying:
    """What the computer is playing, pushed by an agent on the host (host/ in the repo)
    through `adb forward`. The cover is stored once per track (art_key) and served
    separately, so the frequent position updates stay small."""

    def __init__(self, ui):
        self.ui = ui
        self.lock = threading.Lock()
        self.meta = None          # dict for the UI, or None when nothing is playing
        self.art = None           # (key, content type, bytes)
        self.updated = 0.0        # monotonic time of the last update

    def update(self, body):
        """Store an update from the agent. Returns True if it should send the cover."""
        meta = {k: body[k] for k in NOWPLAYING_FIELDS if body.get(k) is not None}
        if not meta.get('title'):
            self.clear()
            return False
        key = str(body.get('art_key') or '')
        with self.lock:
            if key and body.get('art'):
                try:
                    self.art = (key, str(body.get('art_type') or 'image/jpeg'), base64.b64decode(body['art']))
                except (ValueError, TypeError) as e:
                    log('now playing: bad cover data:', e)
            meta['art'] = key if self.art and self.art[0] == key else None
            self.meta = meta
            self.updated = time.monotonic()
        self.ui.publish('nowplaying', meta)
        return bool(key) and meta['art'] is None

    def clear(self):
        with self.lock:
            if self.meta is None:
                return
            self.meta = None
        self.ui.publish('nowplaying', None)

    def expire(self):
        if self.meta is not None and time.monotonic() - self.updated > NOWPLAYING_TTL:
            log('now playing: no update for %d s, clearing' % NOWPLAYING_TTL)
            self.clear()


STATS_TTL = 10          # seconds without stats before the host agent counts as gone
STATS_HISTORY = 60      # samples kept for the graphs (2 minutes at one every 2 s)
STATS_FIELDS = ('cpu', 'cpu_temp', 'mem_used_mb', 'mem_total_mb', 'gpu', 'gpu_temp', 'gpu_mem_used_mb',
                'gpu_mem_total_mb', 'net_rx', 'net_tx')


class HostStats:
    """CPU, memory, GPU and network of the computer, sent by the host agent every 2 s,
    with a short history for the graphs."""

    def __init__(self, ui, on_toast):
        self.ui = ui
        self.on_toast = on_toast
        self.latest = None
        self.history = {k: collections.deque(maxlen=STATS_HISTORY) for k in ('cpu', 'mem', 'gpu', 'net_rx', 'net_tx')}
        self.updated = None       # monotonic time of the last update
        self.pending_kill = None  # ({pid, name, app}, monotonic time) for the agent's next update

    def kill(self, pid):
        """The kill switch: queue the focused app for the agent, if it is still the one shown."""
        focus = (self.latest or {}).get('focus') or {}
        if not self.alive():
            return 'The computer agent is not running'
        if focus.get('pid') != pid:
            return 'The focused app changed; check it and try again'
        if focus.get('protected'):
            return '%s is protected' % focus['app']
        self.pending_kill = ({'pid': pid, 'name': focus['name'], 'app': focus['app']}, time.monotonic())
        return None

    def take_kill(self):
        pending, self.pending_kill = self.pending_kill, None
        return pending[0] if pending and time.monotonic() - pending[1] < 10 else None

    def update(self, body):
        latest = {k: body[k] for k in STATS_FIELDS
                  if isinstance(body.get(k), (int, float)) and not isinstance(body.get(k), bool)}
        # The focused app, for the kill switch: {pid, name, app, title, protected}, or {error}.
        focus = body.get('focus')
        if isinstance(focus, dict) and isinstance(focus.get('pid'), int) and isinstance(focus.get('name'), str):
            latest['focus'] = {'pid': focus['pid'], 'name': focus['name'], 'app': str(focus.get('app') or focus['name']),
                               'title': str(focus.get('title') or ''), 'protected': bool(focus.get('protected'))}
        elif isinstance(focus, dict) and focus.get('error'):
            latest['focus'] = {'error': str(focus['error'])}
        result = body.get('kill_result')
        if isinstance(result, dict):
            name = str(result.get('name') or 'the app')
            if result.get('ok'):
                self.on_toast('Killed %s' % name, 'ok')
            else:
                self.on_toast('Could not kill %s: %s' % (name, result.get('error') or 'unknown error'), 'fail')
        mem = latest['mem_used_mb'] * 100.0 / latest['mem_total_mb'] if latest.get('mem_total_mb') else None
        for key, value in (('cpu', latest.get('cpu')), ('mem', mem), ('gpu', latest.get('gpu')),
                           ('net_rx', latest.get('net_rx')), ('net_tx', latest.get('net_tx'))):
            self.history[key].append(None if value is None else round(value, 1))
        self.latest, self.updated = latest, time.monotonic()
        self.ui.publish('stats', self.snapshot())

    def snapshot(self):
        if self.latest is None:
            return None
        return dict(self.latest, history={k: list(v) for k, v in self.history.items()})

    def alive(self):
        return self.updated is not None and time.monotonic() - self.updated <= STATS_TTL

    def expire(self):
        if self.latest is not None and not self.alive():
            self.latest = None
            for h in self.history.values():
                h.clear()
            self.ui.publish('stats', None)


AUDIO_TTL = 10          # seconds without audio updates before the state is dropped


class HostAudio:
    """The computer's volume, mute and audio devices, from the host agent: several times a
    second while they change, and at least every 2 s. A device picked on the Media page is
    queued and goes to the agent in the reply to its next update."""

    def __init__(self, ui):
        self.ui = ui
        self.state = None
        self.lists = {'outputs': [], 'inputs': []}    # the agent sends them only when they change
        self.updated = None
        self.pending = None       # ({id, kind}, monotonic time)

    def update(self, body):
        for key in ('outputs', 'inputs'):
            if isinstance(body.get(key), list):
                self.lists[key] = [{'id': str(d['id']), 'name': str(d.get('name') or d['id'])}
                                   for d in body[key] if isinstance(d, dict) and d.get('id')]
        volume = body.get('volume')
        state = {'volume': volume if isinstance(volume, int) and not isinstance(volume, bool) else None,
                 'muted': bool(body.get('muted')),
                 'output': body['output'] if isinstance(body.get('output'), str) else None,
                 'input': body['input'] if isinstance(body.get('input'), str) else None,
                 'outputs': self.lists['outputs'], 'inputs': self.lists['inputs']}
        if state != self.state:
            self.state = state
            self.ui.publish('audio', state)
        self.updated = time.monotonic()
        pending, self.pending = self.pending, None
        return {'ok': True, 'set': pending[0] if pending and time.monotonic() - pending[1] < 10 else None,
                'need_lists': not any(self.lists.values())}

    def choose(self, device_id):
        """Queue a device to become the default; returns its name, or None if unknown."""
        for kind in ('output', 'input'):
            for d in self.lists[kind + 's']:
                if d['id'] == device_id:
                    self.pending = ({'id': device_id, 'kind': kind}, time.monotonic())
                    return d['name']
        return None

    def expire(self):
        if self.state is not None and time.monotonic() - self.updated > AUDIO_TTL:
            self.state = None
            self.ui.publish('audio', None)


HUE_OPS = ('pair', 'group', 'light', 'scene', 'all_off')
HUE_STEP = 8            # brightness % per dial detent


class HostHue:
    """Philips Hue, through the host agent: the Car Thing has no network, so the agent
    talks to the Hue Bridge and posts its rooms, zones, lights and scenes here. Commands
    from the UI and the dial wait in a queue the agent collects several times a second
    (GET /api/commands); changes to the same thing merge, so a fast dial turn sends one
    brightness, not dozens. The UI is updated right away and corrected by the next state."""

    def __init__(self, ui):
        self.ui = ui
        self.lock = threading.Lock()
        self.published = {'state': 'unknown'}
        self.commands = []
        self.selected = None      # the room/zone the dial controls

    def update(self, body):
        state = str(body.get('state') or 'error')
        data = {'state': state, 'error': str(body['error']) if body.get('error') else None,
                'rooms': body.get('rooms') if isinstance(body.get('rooms'), list) else []}
        with self.lock:
            self.published = data
        self.publish()

    def publish(self):
        self.ui.publish('hue', self.snapshot())

    def snapshot(self):
        return dict(self.published, selected=self.selected)

    def room(self, rid):
        return next((r for r in self.published.get('rooms') or [] if r.get('id') == rid), None)

    def command(self, cmd):
        """Queue a command from the UI: {op, id?, on?, brightness?, mirek?}."""
        if not isinstance(cmd, dict) or cmd.get('op') not in HUE_OPS:
            raise ValueError('bad hue command')
        cmd = {k: cmd[k] for k in ('op', 'id', 'on', 'brightness', 'mirek') if k in cmd}
        with self.lock:
            same = next((c for c in self.commands if c['op'] == cmd['op'] and c.get('id') == cmd.get('id')
                         and cmd['op'] in ('group', 'light')), None)
            if same:
                same.update(cmd)
            else:
                self.commands.append(cmd)
            self.optimistic(cmd)
        self.publish()

    def optimistic(self, cmd):
        """Show a change before the bridge confirms it."""
        targets = []
        for room in self.published.get('rooms') or []:
            if cmd['op'] == 'group' and room.get('group') == cmd.get('id'):
                targets.append(room)
            if cmd['op'] == 'all_off':
                targets.append(room)
            if cmd['op'] == 'light':
                targets += [l for l in room.get('lights') or [] if l.get('id') == cmd.get('id')]
        for t in targets:
            if cmd['op'] == 'all_off':
                t['on'] = False
            if 'on' in cmd:
                t['on'] = bool(cmd['on'])
            if 'brightness' in cmd:
                t['brightness'] = cmd['brightness']

    def take(self):
        with self.lock:
            commands, self.commands = self.commands, []
        return commands

    def select(self, rid):
        if self.room(rid):
            self.selected = rid
            self.publish()

    def action(self, what):
        """The dial or a tile: {"hue": "up" | "down" | "toggle" | "all_off"} on the selected room."""
        if what == 'all_off':
            return self.command({'op': 'all_off'})
        rooms = self.published.get('rooms') or []
        room = self.room(self.selected) or (rooms[0] if rooms else None)
        if not room or not room.get('group'):
            raise ValueError('no Hue room yet')
        self.selected = room['id']
        brightness = float(room.get('brightness') or 0) if room.get('on') else 0.0
        if what == 'toggle':
            self.command({'op': 'group', 'id': room['group'], 'on': not room.get('on')})
        elif what in ('up', 'down'):
            target = brightness + (HUE_STEP if what == 'up' else -HUE_STEP)
            if target < 1:
                self.command({'op': 'group', 'id': room['group'], 'on': False})
            else:
                self.command({'op': 'group', 'id': room['group'], 'on': True, 'brightness': min(100.0, target)})
        else:
            raise ValueError('unknown hue action %r' % what)
        self.ui.publish('hue_dial', {'room': room['id']})


AGENDA_WEEKS = 4        # the most the Calendar page shows (its weeks button: 1 to 4)
AGENDA_DAYS = AGENDA_WEEKS * 7      # today and the next days
AGENDA_MAX = 300        # events sent to the UI
EPOCH = datetime.datetime(1970, 1, 1)


class Agenda:
    """Upcoming events from the calendars the host agent downloads (calendars.txt on the
    computer). The agent passes the iCal files on as they are; ics.py reads them here,
    so repeats and time zones are handled once for every OS. Meeting links open on the
    computer: the agent picks them up in the reply to its next stats update."""

    def __init__(self, ui, host, opener):
        self.ui, self.host = ui, host
        self.calendar = None      # ics.Calendar
        self.state = 'unknown'    # unknown (no word from the agent) | none (no calendars.txt) | ok
        self.error = None
        self.links = {}           # event id -> meeting link
        self.details = {}         # event id -> {notes, calendar}, fetched when an event is opened
        self.opener = opener
        self.built = None
        self.published = {'state': 'unknown', 'error': None, 'events': []}

    def update(self, text, none, error):
        calendar = None
        if not none:
            started = time.monotonic()
            try:
                since = datetime.datetime.utcfromtimestamp(time.time()) if time.time() > VALID_TIME else None
                calendar = ics.Calendar(text, since)
                log('calendar: %d KB, %d events kept, read in %.1f s' % (len(text) // 1024, len(calendar.events),
                                                                      time.monotonic() - started))
            except Exception as e:     # a malformed file must not take the service down
                log('calendar: could not read it:', e)
                error = error or 'Could not read the calendar: %s' % e
        self.calendar, self.state, self.error = calendar, 'none' if none else 'ok', error or None
        self.refresh(force=True)

    def needs_calendar(self):
        return self.state == 'unknown'

    def refresh(self, force=False):
        """Recompute the upcoming events: on new data, every 5 minutes, and when the UTC offset changes."""
        now = time.time()
        key = (int(now // 300), self.host.utc_offset)
        if key == self.built and not force:
            return
        self.built = key
        events, links, details = [], {}, {}
        if self.calendar is not None and now > VALID_TIME:
            offset = datetime.timedelta(seconds=self.host.utc_offset or 0)
            local_midnight = (datetime.datetime.utcfromtimestamp(now) + offset).replace(
                hour=0, minute=0, second=0, microsecond=0)
            start = local_midnight - offset
            try:
                found = self.calendar.between(start, start + datetime.timedelta(days=AGENDA_DAYS), offset)
            except Exception as e:
                log('calendar: could not list events:', e)
                found = []
            for ev in found[:AGENDA_MAX]:
                start_ms = int((ev['start'] - EPOCH).total_seconds() * 1000)
                eid = '%s|%d' % (ev['uid'], start_ms)
                names = self.calendar.names
                details[eid] = {'notes': ev['notes'], 'calendar': names[ev['cal']] if ev['cal'] < len(names) else ''}
                if ev['link']:
                    links[eid] = ev['link']
                events.append({'id': eid, 'start': start_ms, 'end': int((ev['end'] - EPOCH).total_seconds() * 1000),
                               'all_day': ev['all_day'], 'title': ev['title'], 'where': ev['where'],
                               'link': bool(ev['link']), 'cal': ev['cal']})
        self.links, self.details = links, details
        data = {'state': self.state, 'error': self.error, 'events': events, 'calendars': self.calendar_names()}
        if data != self.published:
            self.published = data
            self.ui.publish('agenda', data)

    def calendar_names(self):
        """A name for each calendar, by the events' 'cal' index, for the Calendar page's tabs:
        its X-WR-CALNAME, "Calendar 2" without one, and the number added to a name used twice."""
        names = []
        for i, name in enumerate(self.calendar.names if self.calendar is not None else []):
            name = name or 'Calendar %d' % (i + 1)
            names.append('%s (%d)' % (name, i + 1) if name in names else name)
        return names

    def open(self, eid):
        link = self.links.get(eid)
        if link:
            self.opener.queue(link)
        return bool(link)


class LinkOpener:
    """A link to open on the computer (a meeting, a GitHub page). It goes to the agent in
    the reply to its next stats update; the agent opens only the kinds of link it knows."""

    def __init__(self):
        self.pending = None       # (link, monotonic time)

    def queue(self, link):
        self.pending = (link, time.monotonic())

    def take(self):
        pending, self.pending = self.pending, None
        return pending[0] if pending and time.monotonic() - pending[1] < 15 else None


GITHUB_FIELDS_RUN = ('workflow', 'status', 'conclusion', 'branch', 'title', 'event', 'created', 'updated')
GITHUB_FIELDS_PR = ('repo', 'number', 'title', 'draft', 'checks', 'review', 'updated')


class GitHubStatus:
    """A repo's latest workflow runs and the user's open pull requests, which the agent
    reads with the gh CLI (the repo is set in github.txt on the computer). Rows open on
    the computer when tapped; their links stay here and the UI only gets ids."""

    def __init__(self, ui, opener):
        self.ui, self.opener = ui, opener
        self.links = {}
        self.published = {'state': 'unknown'}
        self.refresh = False      # a refresh asked for on the Car Thing, for the agent's next command poll

    def update(self, body):
        if body.get('none'):
            data = {'state': 'none'}
        else:
            runs, prs, links = [], [], {}
            for i, run in enumerate(body.get('runs') or []):
                if isinstance(run, dict):
                    runs.append(dict({k: run.get(k) for k in GITHUB_FIELDS_RUN}, id='run%d' % i))
                    links['run%d' % i] = str(run.get('url') or '')
            for i, pr in enumerate(body.get('prs') or []):
                if isinstance(pr, dict):
                    prs.append(dict({k: pr.get(k) for k in GITHUB_FIELDS_PR}, id='pr%d' % i))
                    links['pr%d' % i] = str(pr.get('url') or '')
            self.links = links
            data = {'state': 'ok', 'repo': str(body.get('repo') or ''), 'runs': runs, 'prs': prs,
                    'error': str(body['error']) if body.get('error') else None, 'at': int(time.time() * 1000)}
        self.published = data
        self.ui.publish('github', data)

    def open(self, row):
        link = self.links.get(row)
        if link:
            self.opener.queue(link)
        return bool(link)


PLEX_TTL = 75           # seconds without an update before the Plex data counts as gone (off screen: every 30 s)
PLEX_POSTERS = 16       # posters kept for the streams list
PLEX_FIELDS = ('server', 'sessions', 'load', 'net', 'libraries', 'activities')


class HostPlex:
    """A Plex Media Server on the local network, through the host agent (the Car Thing has
    no network): its streams, load, bandwidth and libraries, sent every 3 s. Posters come
    once per key, when the reply asks for them, and are served separately like the
    now-playing cover, so the updates stay small."""

    def __init__(self, ui):
        self.ui = ui
        self.lock = threading.Lock()
        self.published = {'state': 'unknown'}
        self.posters = collections.OrderedDict()     # key -> (content type, bytes), oldest first
        self.updated = None

    def update(self, body):
        """Store an update from the agent. Returns the poster keys it should send."""
        posters = body.get('posters') if isinstance(body.get('posters'), dict) else {}
        data = {k: body[k] for k in PLEX_FIELDS if k in body}
        data['state'] = str(body.get('state') or 'error')
        data['error'] = str(body['error']) if body.get('error') else None
        data['sessions'] = [s for s in body.get('sessions') or [] if isinstance(s, dict)]
        with self.lock:
            for key, art in posters.items():
                try:
                    raw = base64.b64decode(art)
                except (ValueError, TypeError) as e:
                    log('plex: bad poster data:', e)
                    continue
                self.posters[str(key)] = ('image/png' if raw[:4] == b'\x89PNG' else 'image/jpeg', raw)
                while len(self.posters) > PLEX_POSTERS:
                    self.posters.popitem(last=False)
            need = sorted(set(str(s['art']) for s in data['sessions'] if s.get('art') and str(s['art']) not in self.posters))
            for s in data['sessions']:        # the UI shows a poster only once it is here
                s['poster'] = bool(s.get('art')) and str(s['art']) in self.posters
            self.published, self.updated = data, time.monotonic()
        self.ui.publish('plex', data)
        return need

    def poster(self, key):
        with self.lock:
            return self.posters.get(key)

    def expire(self):
        if self.updated is not None and time.monotonic() - self.updated > PLEX_TTL:
            self.updated = None
            self.published = {'state': 'unknown'}
            self.ui.publish('plex', self.published)


TRUENAS_TTL = 150       # seconds without an update before the TrueNAS data counts as gone (off screen: every 60 s)
TRUENAS_FIELDS = ('system', 'pools', 'disks', 'alerts', 'apps', 'jobs')
CRON_NAMES = {name: i for names in (('sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'),
                                     ('', 'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'))
              for i, name in enumerate(names) if name}


def cron_values(field, lo, hi):
    """The values one cron field allows: *, 5, 1-5, */15, 5/10, lists of those, and day or month names."""
    values = set()
    for part in str(field).lower().split(','):
        part, _, step = part.partition('/')
        step = int(step) if step else 1
        if part in ('*', ''):
            first, last = lo, hi
        elif '-' in part:
            first, last = (int(CRON_NAMES.get(x, x)) for x in part.split('-', 1))
        else:
            first = int(CRON_NAMES.get(part, part))
            last = hi if step > 1 else first
        values.update(range(first, last + 1, step))
    return values


def cron_next(schedule, after):
    """The first time after `after` (a naive local datetime) that a TrueNAS schedule
    {minute, hour, dom, month, dow, begin?, end?} runs, within a year; None if never.
    As in cron, a day matches both dom and dow, or either one when both are set."""
    minutes = sorted(cron_values(schedule.get('minute', '00'), 0, 59))
    hours = sorted(cron_values(schedule.get('hour', '*'), 0, 23))
    doms, months = cron_values(schedule.get('dom', '*'), 1, 31), cron_values(schedule.get('month', '*'), 1, 12)
    dows = {d % 7 for d in cron_values(schedule.get('dow', '*'), 0, 7)}          # 0 and 7 are Sunday
    either = str(schedule.get('dom', '*')) != '*' and str(schedule.get('dow', '*')) != '*'
    begin, end = (tuple(int(x) for x in str(schedule.get(k) or v).split(':')[:2]) for k, v in (('begin', '00:00'), ('end', '23:59')))
    for i in range(366):
        day = after.date() + datetime.timedelta(days=i)
        dom_ok, dow_ok = day.day in doms, (day.weekday() + 1) % 7 in dows
        if day.month not in months or not ((dom_ok or dow_ok) if either else (dom_ok and dow_ok)):
            continue
        for h in hours:
            for m in minutes:
                at = datetime.datetime(day.year, day.month, day.day, h, m)
                if at > after and begin <= (h, m) <= end:
                    return at
    return None


def next_task(tasks, pools, utc_offset):
    """The scheduled task that runs next: {kind, name, at (ms)}, or None. A scrub runs only when
    the pool's last scrub ended more than `threshold` - 1 days before, as in TrueNAS's
    plugins/zpool/scrub_impl.py."""
    offset = datetime.timedelta(seconds=utc_offset or 0)
    now = datetime.datetime.utcfromtimestamp(time.time()) + offset
    scrubbed = {p.get('name'): p.get('scrubbed') for p in pools or []}
    best = None
    for task in tasks or []:
        try:
            at = cron_next(task.get('schedule') or {}, now)
            last = scrubbed.get(task.get('name')) if task.get('kind') == 'scrub' else None
            for _ in range(60):                  # a weekly scrub with a 35-day threshold skips a few weeks
                if at is None or last is None or (at - offset - EPOCH).total_seconds() * 1000 - last > (int(task.get('threshold') or 0) - 1) * 86400000:
                    break
                at = cron_next(task['schedule'], at)
        except (ValueError, TypeError, AttributeError) as e:
            log('truenas: cannot read the schedule of %r: %s' % (task.get('name'), e))
            continue
        if at is not None and (best is None or at < best[0]):
            best = (at, task)
    if best is None:
        return None
    return {'kind': best[1].get('kind'), 'name': best[1].get('name'), 'at': int((best[0] - offset - EPOCH).total_seconds() * 1000)}


class HostTrueNAS:
    """A TrueNAS server on the local network, through the host agent: pools, disk
    temperatures, alerts, apps and running jobs, every 10 s while its page is on screen,
    and the scheduled task that runs next."""

    def __init__(self, ui, host):
        self.ui, self.host = ui, host
        self.published = {'state': 'unknown'}
        self.updated = None

    def update(self, body):
        data = {k: body[k] for k in TRUENAS_FIELDS if k in body}
        data['state'] = str(body.get('state') or 'error')
        data['error'] = str(body['error']) if body.get('error') else None
        if data['state'] == 'ok':
            data['next'] = next_task(body.get('tasks'), body.get('pools'), self.host.utc_offset)
        self.published, self.updated = data, time.monotonic()
        self.ui.publish('truenas', data)

    def expire(self):
        if self.updated is not None and time.monotonic() - self.updated > TRUENAS_TTL:
            self.updated = None
            self.published = {'state': 'unknown'}
            self.ui.publish('truenas', self.published)


class Broadcaster:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []

    def subscribe(self):
        q = queue.Queue(maxsize=64)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, kind, data):
        msg = json.dumps({'type': kind, 'data': data})
        with self.lock:
            for q in self.clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass


def write_backlight(level):
    """Set the backlight to level 5..255. The panel PWM is inverted: the sysfs value is
    260 - level, so 255 is dimmest and 5 brightest."""
    try:
        with open(BACKLIGHT, 'w') as f:
            f.write(str(260 - level))
    except OSError as e:
        log('brightness:', e)


def sensor_level(raw):
    """The brightness level the stock sp-als-backlight service sets for a light sensor
    reading, fitted to its log (within 1 level): 25 in the dark, then up with the log of
    the reading, 210 at most."""
    if raw <= 12:
        return 25.0
    return min(210.0, 63.3 + 19.3 * math.log(raw - 12.2))


# ------------------------------------------------------------------ the pad

class Pad:
    def __init__(self):
        self.ui = Broadcaster()
        self.np = NowPlaying(self.ui)
        self.host = HostLink(lambda: self.publish_state())
        self.stats = HostStats(self.ui, self.toast)
        self.opener = LinkOpener()
        self.agenda = Agenda(self.ui, self.host, self.opener)
        self.github = GitHubStatus(self.ui, self.opener)
        self.audio = HostAudio(self.ui)
        self.hue = HostHue(self.ui)
        self.plex = HostPlex(self.ui)
        self.truenas = HostTrueNAS(self.ui, self.host)
        self.hid = Hid(lambda status: self.on_usb_status(status))
        self.config = {}
        self.config_mtime = 0
        self.page = 0
        self.auto_hidden = set()   # pages hidden because their feature has no config (see refresh_hidden)
        self.dial = 0
        self.mode = 'starting'   # starting | ok | safe | error
        self.message = ''
        self.os_detected = None
        # Device settings changed from the Settings tab; kept in state.json.
        # None means "use config.json".
        self.settings = {'os_mode': 'auto', 'layout': None, 'dial_invert': None,
                         'brightness_auto': True, 'brightness': 180, 'brightness_stops': 0, 'calendar': None,
                         'calendar_weeks': {}, 'night_stops': 0, 'night_from': 22 * 60, 'night_to': 7 * 60,
                         'theme': 'dark', 'accent': 'theme',
                         'clock_24h': True, 'icon_style': None,
                         'icon_color': None, 'page_order': [], 'page_hidden': [], 'page_shown': []}
        self._stock_stopped = False    # the stock light-sensor service, stopped by us
        self._auto_level = None        # auto brightness: the level now, None to jump to the sensor's
        self._dimmed = False
        self._dim_timer = None
        self.settings_open = False     # the screen shows Settings (the page says so, POST /api/view)
        self.lock = threading.RLock()
        self.hid.on_report_request = self.detect_os
        self.load_saved()
        self.load_config()

    # -- host OS: detected from how the host asks for the HID report descriptor.
    # Windows asks for more bytes than the descriptor has; Linux and macOS ask for the
    # exact size, so they look the same here. "other" then means config.other_os, and on
    # a Mac the user picks macOS in Settings.
    def detect_os(self, length):
        detected = 'windows' if length > len(REPORT_DESCRIPTOR) else 'other'
        log('report descriptor requested with wLength=%d -> %s' % (length, detected))
        if detected != self.os_detected:
            self.os_detected = detected
            self.publish_state()

    def host_os(self):
        """'windows', 'mac' or 'linux': selects per-OS action overrides and the layout."""
        if self.settings['os_mode'] != 'auto':
            return self.settings['os_mode']
        if self.os_detected == 'other':
            return self.config.get('other_os', 'linux')
        return self.os_detected or self.config.get('default_os', 'windows')

    def layout_name(self):
        return (self.settings['layout'] or self.config.get('layout_' + self.host_os())
                or self.config.get('layout', 'us'))

    def dial_invert(self):
        if self.settings['dial_invert'] is not None:
            return self.settings['dial_invert']
        return bool(self.config.get('hardware', {}).get('dial_invert'))

    def set_os(self, ref):
        if ref == 'next':
            ref = OS_MODES[(OS_MODES.index(self.settings['os_mode']) + 1) % len(OS_MODES)]
        self.set_setting('os_mode', ref)

    def set_setting(self, key, value):
        valid = {
            'os_mode': lambda v: v in OS_MODES,
            'layout': lambda v: v is None or v in LAYOUTS,
            'dial_invert': lambda v: v is None or isinstance(v, bool),
            'theme': lambda v: v in THEMES,
            'accent': lambda v: v in ACCENTS or (type(v) is int and 0 <= v < 360),
            'clock_24h': lambda v: isinstance(v, bool),
            'icon_style': lambda v: v is None or v in ICON_STYLES,
            'icon_color': lambda v: v is None or v in ICON_COLORS,
            'page_order': lambda v: isinstance(v, list) and len(v) <= 64 and all(isinstance(x, str) for x in v),
            'page_hidden': lambda v: isinstance(v, list) and len(v) <= 64 and all(isinstance(x, str) for x in v),
            'page_shown': lambda v: isinstance(v, list) and len(v) <= 64 and all(isinstance(x, str) for x in v),
            'brightness_auto': lambda v: isinstance(v, bool),
            'brightness': lambda v: isinstance(v, int) and BRIGHTNESS_MIN <= v <= 255,
            'brightness_stops': lambda v: type(v) in (int, float) and abs(v) <= BRIGHTNESS_STOPS and v * 2 == int(v * 2),
            # Darker at night: 0 (off) to -3 stops, from night_from to night_to (minutes of the day).
            'night_stops': lambda v: type(v) in (int, float) and -BRIGHTNESS_STOPS <= v <= 0 and v * 2 == int(v * 2),
            'night_from': lambda v: type(v) is int and 0 <= v < 1440,
            'night_to': lambda v: type(v) is int and 0 <= v < 1440,
            'calendar': lambda v: v is None or (isinstance(v, str) and len(v) <= 200),     # the Calendar page's tab; None: all
            # Weeks the Calendar page shows, per tab: a calendar's name, or '' for All.
            'calendar_weeks': lambda v: isinstance(v, dict) and len(v) <= 64 and all(
                isinstance(k, str) and len(k) <= 200 and type(n) is int and 1 <= n <= AGENDA_WEEKS for k, n in v.items()),
        }
        if key not in valid or not valid[key](value):
            raise ValueError('bad setting %s=%r' % (key, value))
        if key in ('page_order', 'page_hidden', 'page_shown'):    # stay on the same page when tabs move or hide
            current = page_key(self.ordered_pages()[self.page]) if self.page < len(self.ordered_pages()) else None
            self.settings[key] = value
            keys = [page_key(p) for p in self.ordered_pages()]
            self.page = keys.index(current) if current in keys else 0
        self.settings[key] = value
        if key.startswith(('brightness', 'night')):
            self.apply_brightness()
        self.save()
        self.publish_state()

    def apply_brightness(self):
        """Apply the brightness setting (not while dimmed for standby). In auto, follow_sensor
        jumps to the new level on its next reading."""
        if self._dimmed:
            return
        self._stop_stock_sensor()
        self._auto_level = None
        if not self.settings['brightness_auto']:
            write_backlight(self.manual_level())

    def night_active(self):
        """Whether it is night now (Settings → General), by the computer's clock: never before
        the agent has set the time."""
        start, end = self.settings['night_from'], self.settings['night_to']
        if not self.settings['night_stops'] or start == end or self.host.utc_offset is None or time.time() < VALID_TIME:
            return False
        minute = int((time.time() + self.host.utc_offset) // 60 % 1440)
        return start <= minute < end if start < end else minute >= start or minute < end

    def night_stops(self):
        return self.settings['night_stops'] if self.night_active() else 0

    def manual_level(self):
        """The slider's level, darker at night."""
        return max(BRIGHTNESS_MIN, round(self.settings['brightness'] * 2 ** self.night_stops()))

    def _stop_stock_sensor(self):
        # The stock sp-als-backlight service drives the backlight from the light sensor, but
        # it has no offset, so follow_sensor does its job. supervisorctl is slow: only once.
        if not self._stock_stopped:
            subprocess.call(['supervisorctl', 'stop', 'backlight'], stdout=subprocess.DEVNULL)
            self._stock_stopped = True

    def release_backlight(self):
        """On the way out, give auto brightness back to the stock service, so the screen still
        follows the light while this service is down. A manual or standby level stays as it is."""
        if self._stock_stopped and self.settings['brightness_auto'] and not self._dimmed:
            self._stock_stopped = False          # follow_sensor stops writing
            try:
                subprocess.call(['supervisorctl', 'start', 'backlight'], stdout=subprocess.DEVNULL, timeout=5)
            except subprocess.TimeoutExpired:
                log('could not start the stock light-sensor service')

    def follow_sensor(self):
        """The backlight, every 0.5 s. Auto: the stock service's level for the light sensor,
        times 2 for each stop of the offset and of the night; it moves a fifth of the way there
        on each reading, so it fades the way the stock service did and a passing shadow
        doesn't flicker it. Manual: the slider's level, darker at night."""
        paths = glob.glob(LIGHT_SENSOR)
        if not paths:
            log('no light sensor: auto brightness stays where it is')
        written, night = None, None
        while True:
            time.sleep(0.5)
            if self.night_active() != night:
                night = self.night_active()
                self.publish_state()             # Settings shows when it is night
            if self._dimmed or not self._stock_stopped:
                written = None
                continue
            if not self.settings['brightness_auto']:
                level = self.manual_level()
            else:
                try:
                    with open(paths[0]) as f:
                        raw = int(f.read())
                except (IndexError, OSError, ValueError):
                    continue
                stops = self.settings['brightness_stops'] + self.night_stops()
                target = max(BRIGHTNESS_MIN, min(255.0, sensor_level(raw) * 2 ** stops))
                level = target if self._auto_level is None else self._auto_level + (target - self._auto_level) / 5
                self._auto_level = level
            if round(level) != written and not self._dimmed and self._stock_stopped:
                written = round(level)
                write_backlight(written)

    # -- standby dimming: minimum brightness while the computer sleeps or is gone
    def on_usb_status(self, status):
        self.publish_state()
        if self._dim_timer:
            self._dim_timer.cancel()
            self._dim_timer = None
        if self.standby():
            # Same 3 s grace as the standby screen, so re-enumerations don't flicker.
            self._dim_timer = threading.Timer(3, self._dim)
            self._dim_timer.daemon = True
            self._dim_timer.start()
        elif self._dimmed:
            self._dimmed = False
            self.apply_brightness()
            log('computer back: brightness restored')

    def _dim(self):
        if self.standby() and not self._dimmed:
            self._dimmed = True
            self._stop_stock_sensor()
            write_backlight(BRIGHTNESS_MIN)
            log('standby: brightness at minimum')

    def load_saved(self):
        try:
            with open(STATE_PATH) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        for key, value in saved.items():
            if key in self.settings:
                self.settings[key] = value

    def save(self):
        try:
            with self.lock:    # HTTP requests save concurrently (e.g. dragging the brightness slider)
                with open(STATE_PATH + '.tmp', 'w') as f:
                    json.dump(self.settings, f)
                os.rename(STATE_PATH + '.tmp', STATE_PATH)
        except OSError as e:
            log('could not save state:', e)

    def info(self):
        def read(path, default=''):
            try:
                with open(path) as f:
                    return f.read().strip()
            except OSError:
                return default
        mem = {}
        for line in read('/proc/meminfo').splitlines():
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(':')] = int(parts[1])
        return {
            'uptime': float(read('/proc/uptime', '0').split()[0]),
            'temp_c': int(read('/sys/class/thermal/thermal_zone0/temp', '0')) / 1000.0,
            'mem_total_mb': mem.get('MemTotal', 0) // 1024,
            'mem_avail_mb': mem.get('MemAvailable', 0) // 1024,
            'load': read('/proc/loadavg').split()[:3],
            'brightness_now': 260 - int(read(BACKLIGHT, '255') or 255),
            'layouts': sorted(LAYOUTS),
        }

    def variant(self, a):
        """An action may carry per-OS overrides: {"key": "win+tab", "linux": {"key": "win"}}."""
        override = a.get(self.host_os()) if isinstance(a, dict) else None
        if not isinstance(override, dict):
            return a
        merged = {k: a[k] for k in ('label', 'icon', 'symbol', 'color') if k in a}
        merged.update(override)
        return merged

    def load_config(self):
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
            if mtime == self.config_mtime:
                return False
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            log('config error:', e)
            self.toast('config.json: %s' % e, 'fail')
            self.config_mtime = os.path.getmtime(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else 0
            return False
        with self.lock:
            self.config = cfg
            self.config_mtime = mtime
            # Forget tab settings of pages config.json no longer has (not for a config
            # without pages, which is more likely a slip than a choice).
            known = set(page_key(p) for p in cfg.get('pages', []))
            stale = False
            for key in ('page_order', 'page_hidden', 'page_shown') if known else ():
                kept = [k for k in self.settings.get(key) or [] if k in known]
                stale = stale or kept != (self.settings.get(key) or [])
                self.settings[key] = kept
            self.page = min(self.page, max(0, len(self.pages()) - 1))
            self.dial = min(self.dial, max(0, len(self.dials()) - 1))
        if stale:
            self.save()
        log('config loaded: %d pages, %d dial modes' % (len(self.pages()), len(self.dials())))
        return True

    def pages(self):
        """The pages as shown, in the Settings order."""
        return self.ordered_pages()

    def ordered_pages(self, hidden=False):
        """config.json's page entries in the order picked in Settings → Tabs, without the ones
        hidden there (unless hidden=True). Entries the order doesn't name (new in config.json)
        keep their config order, after the named ones. At least one page always shows."""
        entries = self.config.get('pages', [])
        rank = {key: i for i, key in enumerate(self.settings.get('page_order') or [])}
        ordered = [p for _, p in sorted(enumerate(entries), key=lambda e: (rank.get(page_key(e[1]), len(rank)), e[0]))]
        if hidden:
            return ordered
        hidden = self.hidden_keys()
        shown = [p for p in ordered if page_key(p) not in hidden]
        return shown or ordered[:1]

    def hidden_keys(self):
        """Pages hidden in Settings → Tabs, and pages whose feature has no config, unless
        they were turned on there."""
        return set(self.settings.get('page_hidden') or []) | (self.auto_hidden - set(self.settings.get('page_shown') or []))

    def refresh_hidden(self):
        """Hide the pages whose panels all say their config is missing (no calendars.txt,
        github.txt, plex.txt or truenas.txt, Hue not paired), staying on the same page.
        'unknown' (no agent yet) doesn't count, so nothing hides before the agent says so."""
        none = {'agenda': self.agenda.state == 'none', 'lights': self.hue.published.get('state') == 'none',
                'github': self.github.published.get('state') == 'none', 'plex': self.plex.published.get('state') == 'none',
                'truenas': self.truenas.published.get('state') == 'none'}
        auto = set(page_key(p) for p in self.config.get('pages', [])
                   if p.get('widgets') and all(none.get(w) for w in p['widgets']))
        if auto == self.auto_hidden:
            return
        with self.lock:
            pages = self.ordered_pages()
            current = page_key(pages[self.page]) if self.page < len(pages) else None
            self.auto_hidden = auto
            keys = [page_key(p) for p in self.ordered_pages()]
            self.page = keys.index(current) if current in keys else 0
        self.publish_state()


    def dials(self):
        return self.config.get('dial_modes', [])

    def state(self):
        return {'page': self.page, 'dial': self.dial, 'usb': self.hid.status,
                'mode': self.mode, 'message': self.message,
                'os': self.host_os(), 'os_mode': self.settings['os_mode'], 'os_detected': self.os_detected,
                'settings': self.settings, 'layout': self.layout_name(), 'dial_invert': self.dial_invert(),
                'locked': self.host.locked, 'clock': self.host.clock(), 'agent': self.agent_status(),
                'hidden': sorted(self.hidden_keys()), 'unconfigured': sorted(self.auto_hidden),
                'night': self.night_active()}

    def agent_status(self):
        """The agent's version against the repo's (agents.json, written by deploy.sh):
        {os, version, expected, outdated}, or None before the agent's first heartbeat.
        An agent that sends no version is out of date."""
        agent = self.host.agent
        if not agent:
            return None
        try:
            with open(os.path.join(BASE, 'agents.json')) as f:
                expected = json.load(f).get(agent['os'])
        except (OSError, ValueError):
            expected = None
        outdated = agent['version'] is None or bool(expected) and expected != agent['version']
        return dict(agent, expected=expected, outdated=outdated)

    def publish_state(self):
        self.ui.publish('state', self.state())

    def toast(self, text, level='info'):
        self.ui.publish('toast', {'text': text, 'level': level})

    def flash(self, where, index, label=None):
        self.ui.publish('fired', {'where': where, 'page': self.page, 'index': index, 'label': label})

    # -- lookups
    def presets(self):
        page = self.pages()[self.page] if self.pages() else {}
        own = page.get('presets') or []
        default = self.config.get('presets') or []
        return [own[i] if i < len(own) and own[i] else (default[i] if i < len(default) else None)
                for i in range(4)]

    def _resolve(self, items, ref):
        if isinstance(ref, int):
            return ref % len(items) if items else 0
        for i, item in enumerate(items):
            if str(item.get('name', '')).lower() == str(ref).lower():
                return i
        return None

    # -- actions
    def run(self, action):
        if not action:
            return
        global layout
        layout = LAYOUTS.get(self.layout_name(), LAYOUTS['us'])
        if self.host.locked and self._types(action):
            # On a locked computer, keystrokes land in the password box (and Enter
            # counts as a failed sign-in). Media keys, volume and wheel still go through.
            self.toast('Computer locked: only media controls work', 'warn')
            return
        try:
            self._run(action)
        except Exception as e:  # a bad config entry must never kill the input loop
            log('action failed:', action, e)
            self.toast('Action error: %s' % e, 'fail')

    def _types(self, a):
        """Would this action press keyboard keys?"""
        a = self.variant(a)
        if 'seq' in a:
            return any(self._types(step) for step in a['seq'])
        if 'dial_press' in a:
            mode = self.dials()[self.dial] if self.dials() else {}
            return self._types(mode.get('press') or {})
        return 'key' in a or 'type' in a or bool(a.get('mods'))

    def _run(self, a):
        hid = self.hid
        a = self.variant(a)
        if 'seq' in a:
            for step in a['seq']:
                self._run(step)
                time.sleep(float(a.get('delay', 0.03)))
        elif 'key' in a:
            mods, keys = parse_combo(a['key'])
            hid.combo(mods, keys)
        elif 'media' in a:
            name = a['media']
            hid.consumer(MEDIA[name] if isinstance(name, str) else int(name))
        elif 'type' in a:
            hid.type_text(a['type'])
        elif 'wheel' in a or 'hwheel' in a:
            mods = parse_combo(a['mods'])[0] if a.get('mods') else 0
            if mods:
                hid.keyboard(mods, [], 0.01)
            hid.mouse(wheel=int(a.get('wheel', 0)), pan=int(a.get('hwheel', 0)))
            if mods:
                hid.keyboard(0, [])
        elif 'mic_mute' in a:
            hid.mic_mute()
        elif 'click' in a:
            hid.click(MOUSE_BUTTONS[a['click']])
        elif 'page' in a:
            self.set_page(a['page'])
        elif 'dial' in a:
            self.set_dial(a['dial'])
        elif 'dial_press' in a:
            mode = self.dials()[self.dial] if self.dials() else {}
            self._run(mode.get('press') or {})
        elif 'hue' in a:
            self.hue.action(a['hue'])
        elif 'notify' in a:
            self.toast(a['notify'], a.get('level', 'info'))
        else:
            raise ValueError('no action in %r' % (a,))

    def set_page(self, ref):
        with self.lock:
            n = len(self.pages())
            if not n:
                return
            if self.settings_open and ref in ('next', 'prev'):
                ref = 0 if ref == 'next' else -1      # Settings (⚙) is the tab after the last page
            leaving, self.settings_open = self.settings_open, False
            if ref == 'next':
                self.page = (self.page + 1) % n
            elif ref == 'prev':
                self.page = (self.page - 1) % n
            else:
                idx = self._resolve(self.pages(), ref)
                if idx is not None:
                    self.page = idx
        if leaving:
            self.ui.publish('view', {'settings': False})      # the page closes Settings, also on the same page
        self.publish_state()

    def set_dial(self, ref):
        with self.lock:
            n = len(self.dials())
            if not n:
                return
            if ref == 'next':
                self.dial = (self.dial + 1) % n
            elif ref == 'prev':
                self.dial = (self.dial - 1) % n
            else:
                idx = self._resolve(self.dials(), ref)
                if idx is not None:
                    self.dial = idx
        self.publish_state()

    # -- standby: the computer is asleep or off; the screen says so and input is ignored.
    # The Car Thing cannot wake the computer: with the host asleep the Amlogic USB PHY is
    # in low-power mode and never drives the resume signal, and asking the controller to
    # send it locked it up until a power cycle. So nothing touches the controller here.
    def visible_widgets(self):
        """The live panels on screen ("lights", "monitor", "github", …): the agent updates those
        often and the rest slowly. None on the standby or lock screen."""
        if self.standby() or self.host.locked:
            return []
        pages = self.pages()
        return list(pages[self.page].get('widgets') or []) if self.page < len(pages) else []

    def standby(self):
        return self.mode == 'ok' and self.hid.status != 'connected'

    # -- triggers
    def tap_tile(self, page, index):
        pages = self.pages()
        if 0 <= page < len(pages):
            tiles = pages[page].get('tiles', [])
            if 0 <= index < len(tiles) and tiles[index]:
                self.flash('tile', index, tiles[index].get('label'))
                self.run(tiles[index])

    def press_preset(self, index, hold=False):
        """A top button: its action, or with hold=True its "hold" action (pressed HOLD_SECONDS or longer)."""
        if self.standby():
            return
        preset = self.presets()[index] if 0 <= index < 4 else None
        action = preset.get('hold') if preset and hold else preset
        if action:
            self.flash('preset', index, action.get('label') or preset.get('label'))
            self.run(action)

    def preset_holds(self, index):
        """Whether a top button has a "hold" action: then its tap fires on release."""
        preset = self.presets()[index] if 0 <= index < 4 else None
        return bool(preset and preset.get('hold'))

    def turn(self, steps):
        if self.standby():
            return
        if self.dial_invert():
            steps = -steps
        mode = self.dials()[self.dial] if self.dials() else {}
        action = mode.get('cw') if steps > 0 else mode.get('ccw')
        for _ in range(abs(steps)):
            self.run(action)
        self.ui.publish('turn', {'dir': 1 if steps > 0 else -1})

    def hardware(self, name, kind):
        """name: back|menu|knob, kind: tap|hold."""
        if self.standby():
            return
        binding = self.config.get('hardware', {}).get(name, {})
        action = binding.get(kind)
        if action:
            self.ui.publish('hw', {'button': name, 'kind': kind})
            self.run(action)


# ------------------------------------------------------------- hardware input

EV_KEY, EV_REL = 1, 2
REL_HWHEEL = 6
KEY_ESC, KEY_1, KEY_2, KEY_3, KEY_4, KEY_ENTER, KEY_M = 1, 2, 3, 4, 5, 28, 50
EVENT_FMT = 'llHHi'
EVENT_SIZE = struct.calcsize(EVENT_FMT)
HOLD_SECONDS = 0.45


def find_input(name):
    dev = None
    with open('/proc/bus/input/devices') as f:
        for line in f:
            if line.startswith('N: Name='):
                dev = line.strip()[len('N: Name='):].strip('"')
            elif line.startswith('H: Handlers=') and dev == name:
                for h in line.split('=', 1)[1].split():
                    if h.startswith('event'):
                        return '/dev/input/' + h
    return None


def key_held(path, code):
    """EVIOCGKEY: is `code` currently pressed on this device?"""
    if not path:
        return False
    buf = bytearray(96)
    req = (2 << 30) | (len(buf) << 16) | (ord('E') << 8) | 0x18
    try:
        with open(path, 'rb') as f:
            fcntl.ioctl(f, req, buf)
    except OSError:
        return False
    return bool(buf[code // 8] & (1 << (code % 8)))


def input_loop(pad, keys_path, rotary_path):
    fds = {}
    for path in (keys_path, rotary_path):
        if path:
            fds[os.open(path, os.O_RDONLY)] = path
    presets = {KEY_1: 0, KEY_2: 1, KEY_3: 2, KEY_4: 3}
    named = {KEY_ESC: 'back', KEY_M: 'menu', KEY_ENTER: 'knob'}
    down = {}   # code -> (deadline, hold fired), for buttons with a hold action

    def has_hold(code):
        return code in named and pad.config.get('hardware', {}).get(named[code], {}).get('hold')

    while True:
        timeout = None
        pending = [d for d, fired in down.values() if not fired]
        if pending:
            timeout = max(0.0, min(pending) - time.monotonic())
        ready, _, _ = select.select(list(fds), [], [], timeout)
        now = time.monotonic()
        for code, (deadline, fired) in list(down.items()):
            if not fired and now >= deadline:
                down[code] = (deadline, True)
                if code in presets:
                    pad.press_preset(presets[code], hold=True)
                else:
                    pad.hardware(named[code], 'hold')
        for fd in ready:
            data = os.read(fd, EVENT_SIZE * 32)
            for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, etype, code, value = struct.unpack_from(EVENT_FMT, data, off)
                if etype == EV_REL and code == REL_HWHEEL and value:
                    pad.turn(value)
                elif etype == EV_KEY and value in (0, 1):
                    if code in presets:
                        if value == 1:
                            if pad.preset_holds(presets[code]):
                                down[code] = (now + HOLD_SECONDS, False)
                            else:
                                pad.press_preset(presets[code])
                        elif code in down:
                            _, fired = down.pop(code)
                            if not fired:
                                pad.press_preset(presets[code])
                    elif code in named:
                        if value == 1:
                            if has_hold(code):
                                down[code] = (now + HOLD_SECONDS, False)
                            else:
                                pad.hardware(named[code], 'tap')
                        elif code in down:
                            _, fired = down.pop(code)
                            if not fired:
                                pad.hardware(named[code], 'tap')
                    else:
                        log('unmapped key', code, value)


# ----------------------------------------------------------------- web UI

class Handler(BaseHTTPRequestHandler):
    pad = None
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype='application/json', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        headers = {'Content-Type': ctype, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store',
                   'Access-Control-Allow-Origin': '*'}    # web/shim.html pings from file://
        headers.update(extra or {})                       # extra headers replace these defaults
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, 'rb') as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._send(404, {'error': 'not found'})

    def do_GET(self):
        pad = self.pad
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            self._file(os.path.join(BASE, 'web', 'index.html'), 'text/html; charset=utf-8')
        elif path.startswith('/fonts/') and '..' not in path:
            self._file(os.path.join(WEBAPP_DIR, path.lstrip('/')), 'font/ttf')
        elif path == '/api/ping':
            self._send(200, {'ok': True})
        elif path == '/api/info':
            self._send(200, pad.info())
        elif path == '/api/state':
            self._send(200, {'config': pad.config, 'state': pad.state(), 'presets': pad.presets(),
                             'nowplaying': pad.np.meta, 'stats': pad.stats.snapshot(), 'agenda': pad.agenda.published,
                             'audio': pad.audio.state, 'github': pad.github.published,
                             'hue': pad.hue.snapshot(), 'plex': pad.plex.published, 'truenas': pad.truenas.published})
        elif path == '/api/nowplaying':
            self._send(200, {'nowplaying': pad.np.meta})
        elif path == '/api/nowplaying/art':
            art = pad.np.art
            if art:
                self._send(200, art[2], art[1])
            else:
                self._send(404, {'error': 'no cover'})
        elif path == '/api/plex/art':
            key = (urllib.parse.parse_qs(self.path.partition('?')[2]).get('k') or [''])[0]
            poster = pad.plex.poster(key)
            if poster:      # a key always names the same image, so redrawn rows don't load it again
                self._send(200, poster[1], poster[0], {'Cache-Control': 'max-age=86400'})
            else:
                self._send(404, {'error': 'no poster'})
        elif path == '/api/calendar/event':
            eid = (urllib.parse.parse_qs(self.path.partition('?')[2]).get('id') or [''])[0]
            self._send(200, pad.agenda.details.get(eid) or {'notes': '', 'calendar': ''})
        elif path == '/api/commands':
            refresh, pad.github.refresh = pad.github.refresh, False
            self._send(200, {'hue': pad.hue.take(), 'github_refresh': refresh,   # the agent collects these several times a second
                             'visible': pad.visible_widgets()})
        elif path == '/api/events':
            self._events()
        else:
            self._send(404, {'error': 'not found'})

    def _events(self):
        q = self.pad.ui.subscribe()
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            self.wfile.write(b'retry: 1000\n\n')
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(('data: %s\n\n' % msg).encode())
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.pad.ui.unsubscribe(q)
            self.close_connection = True

    def do_POST(self):
        pad = self.pad
        length = int(self.headers.get('Content-Length') or 0)
        path, _, query = self.path.partition('?')
        if path == '/api/calendar':
            # The iCal files as they are (not JSON): ?none=1 means no calendars.txt, ?error= a download error.
            if length > 32 * 1024 * 1024:
                return self._send(413, {'error': 'calendar too large'})
            params = urllib.parse.parse_qs(query)
            pad.agenda.update(self.rfile.read(length).decode('utf-8', 'replace'), 'none' in params,
                              (params.get('error') or [None])[0])
            return self._send(200, {'ok': True})
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._send(400, {'error': 'expected a JSON object'})
        if path == '/api/tap':
            threading.Thread(target=pad.tap_tile, args=(int(body.get('page', -1)), int(body.get('index', -1))),
                             daemon=True).start()
        elif path == '/api/preset':
            pad.press_preset(int(body.get('index', -1)))
        elif path == '/api/page':
            pad.set_page(body.get('page', 'next'))
        elif path == '/api/view':
            pad.settings_open = bool(body.get('settings'))
        elif path == '/api/dial':
            pad.set_dial(body.get('dial', 'next'))
        elif path == '/api/settings':
            try:
                for key, value in body.items():
                    pad.set_setting(key, value)
            except ValueError as e:
                return self._send(400, {'error': str(e)})
        elif path == '/api/system':
            threading.Thread(target=system_action, args=(body.get('do'), pad), daemon=True).start()
        elif path == '/api/os':
            pad.set_os(body.get('os', 'next'))
        elif path == '/api/nowplaying':
            return self._send(200, {'ok': True, 'need_art': pad.np.update(body)})
        elif path == '/api/host':
            pad.host.update(body)
        elif path == '/api/stats':
            pad.stats.update(body)
            return self._send(200, {'ok': True, 'open': pad.opener.take(), 'kill': pad.stats.take_kill(),
                                    'need_calendar': pad.agenda.needs_calendar()})
        elif path == '/api/hue':
            pad.hue.update(body)
        elif path == '/api/hue/command':
            try:
                pad.hue.command(body)
            except ValueError as e:
                return self._send(400, {'error': str(e)})
        elif path == '/api/hue/select':
            pad.hue.select(str(body.get('id')))
        elif path == '/api/plex':
            return self._send(200, {'ok': True, 'need_art': pad.plex.update(body)})
        elif path == '/api/truenas':
            pad.truenas.update(body)
        elif path == '/api/github':
            pad.github.update(body)
        elif path == '/api/github/refresh':
            pad.github.refresh = True
        elif path == '/api/github/open':
            if not pad.stats.alive():
                pad.toast('The computer agent is not running', 'warn')
            elif pad.github.open(str(body.get('id'))):
                pad.toast('Opening on the computer…')
        elif path == '/api/audio':
            return self._send(200, pad.audio.update(body))
        elif path == '/api/audio/set':
            name = pad.audio.choose(str(body.get('id')))
            pad.toast('Switching to %s…' % name if name else 'That audio device is gone', 'info' if name else 'warn')
        elif path == '/api/kill':
            problem = pad.stats.kill(body.get('pid'))
            if problem:
                pad.toast(problem, 'warn')
            else:
                pad.toast('Killing %s…' % pad.stats.pending_kill[0]['app'])
        elif path == '/api/calendar/open':
            if not pad.stats.alive():
                pad.toast('The computer agent is not running', 'warn')
            elif pad.agenda.open(str(body.get('id'))):
                pad.toast('Opening the meeting on the computer…')
        elif path == '/api/run':
            pad.run(body)
        else:
            return self._send(404, {'error': 'not found'})
        self._send(200, {'ok': True})


def reconnect_usb(pad):
    """Software replug: detach from USB and attach again. If the controller is wedged and
    the host still has not configured us 20 s later, reboot, which resets the USB hardware
    like pulling the cable does."""
    log('usb reconnect requested')
    pad.toast('Reconnecting USB…')
    try:
        with open('/sys/kernel/config/usb_gadget/g1/UDC', 'w') as f:
            f.write('\n')
    except OSError as e:
        log('unbind:', e)
    time.sleep(1.5)
    # usb_watchdog rebinds it, the same clean way it recovers from a bus reset.
    for _ in range(20):
        time.sleep(1)
        if pad.hid.status == 'connected':
            log('usb reconnected')
            pad.toast('USB reconnected', 'ok')
            return
    log('still not connected after reconnect, rebooting (same as unplugging)')
    pad.toast('Still not connected: restarting the Car Thing', 'warn')
    time.sleep(1.5)
    subprocess.call(['sync'])
    subprocess.call(['reboot'])


def system_action(what, pad):
    time.sleep(0.5)          # let the HTTP reply go out first
    if what == 'reconnect':
        reconnect_usb(pad)
    elif what == 'restart':
        log('restart requested from settings')
        os.kill(os.getpid(), signal.SIGTERM)
    elif what == 'reboot':
        log('reboot requested from settings')
        subprocess.call(['sync'])
        subprocess.call(['reboot'])


def serve_ui(pad):
    Handler.pad = pad
    server = ThreadingHTTPServer(('127.0.0.1', HTTP_PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name='http', daemon=True).start()
    log('ui on http://127.0.0.1:%d/' % HTTP_PORT)


def take_over_kiosk():
    """Point the stock kiosk browser at our UI (runtime only; the rootfs is untouched)."""
    target = os.path.join(WEBAPP_DIR, 'index.html')
    shim = os.path.join(BASE, 'web', 'shim.html')
    with open('/proc/mounts') as f:
        mounted = any(line.split()[1] == target for line in f if len(line.split()) > 1)
    if not mounted:
        subprocess.call(['mount', '--bind', shim, target])
    subprocess.call(['supervisorctl', 'restart', 'chromium'], stdout=subprocess.DEVNULL)
    log('kiosk now shows the macro pad')
    wait_for_boot_marked_successful()
    subprocess.call(['supervisorctl', 'stop', 'superbird'], stdout=subprocess.DEVNULL)
    log('stock superbird app stopped')


def boot_slot_marked():
    """Is the running A/B slot marked successful? The bootloader's A/B data sits in
    /dev/misc at offset 2048: magic "\\0AB0", version, then 4 bytes per slot
    (priority, tries left, successful, reserved) for slot a at +8 and slot b at +12.
    Returns None if it can't be read."""
    try:
        with open('/proc/cmdline') as f:
            slot = f.read().split('androidboot.slot_suffix=_', 1)[1][:1]
        with open('/dev/misc', 'rb') as f:
            f.seek(2048)
            data = f.read(16)
    except (OSError, IndexError):
        return None
    if data[:4] != b'\0AB0' or slot not in 'ab':
        return None
    return data[(8 if slot == 'a' else 12) + 2] == 1


def wait_for_boot_marked_successful(timeout=120):
    """Every boot clears the running slot's "successful" flag and spends one of its tries;
    after 7 unmarked boots the bootloader falls back to the other slot. On this system
    qt-superbird-app sets the flag, ~25 s after it starts. Stopping it earlier made the
    device switch slots and lose the boot hook. So let it run until the flag is set."""
    with open('/proc/uptime') as f:
        if float(f.read().split()[0]) > 180:
            return            # booted long ago (service restart): already marked
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if boot_slot_marked():
            log('boot slot marked successful')
            return
        time.sleep(2)
    log('boot slot still not marked after %d s; stopping the stock app anyway' % timeout)


def usb_watchdog(pad):
    """adbd closes and reopens its FunctionFS files after a bus reset (the PC resuming from
    sleep, for example). With configfs that unbinds the whole gadget, and nothing rebinds
    it: the device silently drops off USB. Rebind once both functions are ready again."""
    udc_file = '/sys/kernel/config/usb_gadget/g1/UDC'
    udc = os.listdir('/sys/class/udc')[0]
    rebinds = []                      # monotonic times of recent rebinds
    while True:
        time.sleep(1)
        try:
            with open(udc_file) as f:
                if f.read().strip():
                    continue
        except OSError:
            continue
        now = time.monotonic()
        rebinds = [t for t in rebinds if now - t < 30]
        if len(rebinds) >= 3:
            # Rebinding right after adbd's reopen made it reopen again, in a loop.
            log('gadget keeps unbinding; backing off for 30 s')
            time.sleep(30)
            rebinds = []
            continue
        # Same order as a clean start (gadget.sh): fresh adbd, wait until it has written
        # its descriptors and settled, then bind. A half-reopened adbd unbinds us again.
        log('gadget was unbound (bus reset?), restarting adbd and rebinding')
        subprocess.call(['killall', 'adbd'], stderr=subprocess.DEVNULL)
        time.sleep(1)
        subprocess.Popen(['setsid', '/usr/bin/adbd'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(15):
            time.sleep(1)
            if os.path.exists('/dev/usb-ffs/adb/ep1') and os.path.exists(os.path.join(FFS_DIR, 'ep1')):
                break
        time.sleep(1)
        rebinds.append(time.monotonic())
        try:
            with open(udc_file) as f:
                if f.read().strip():
                    continue
            with open(udc_file, 'w') as f:
                f.write(udc)
        except OSError as e:
            log('rebind failed:', e)


# ------------------------------------------------------------------- main

def gadget(*args):
    rc = subprocess.call(['sh', GADGET_SH] + list(args))
    if rc != 0:
        raise RuntimeError('gadget.sh %s failed (%d)' % (' '.join(args), rc))


def main():
    pad = Pad()
    serve_ui(pad)
    pad.apply_brightness()
    threading.Thread(target=pad.follow_sensor, daemon=True).start()
    if pad.config.get('kiosk', True) and '--no-kiosk' not in sys.argv:
        threading.Thread(target=take_over_kiosk, daemon=True).start()

    keys_path = find_input('gpio-keys')
    rotary_path = find_input('rotary@0')
    log('inputs:', keys_path, rotary_path)

    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)

    try:
        if key_held(keys_path, KEY_ESC):
            pad.mode, pad.message = 'safe', 'Safe mode: back button held at start, USB left as ADB only'
            log(pad.message)
        else:
            try:
                gadget('prepare')
                pad.hid.open()
                gadget('bind-hid')
                pad.mode = 'ok'
                threading.Thread(target=usb_watchdog, args=(pad,), name='usb-watchdog', daemon=True).start()
            except Exception as e:
                log('HID setup failed:', e)
                pad.hid.close()
                subprocess.call(['sh', GADGET_SH, 'adb-only'])
                pad.mode, pad.message = 'error', 'USB HID setup failed: %s' % e
        pad.on_usb_status(pad.hid.status)     # publishes state; dims if no computer is there yet
        threading.Thread(target=input_loop, args=(pad, keys_path, rotary_path), name='input', daemon=True).start()
        while True:
            time.sleep(2)
            if pad.load_config():
                pad.ui.publish('config', {})
            pad.np.expire()
            pad.host.expire()
            pad.stats.expire()
            pad.audio.expire()
            pad.plex.expire()
            pad.truenas.expire()
            pad.refresh_hidden()
            pad.agenda.refresh()
    finally:
        pad.release_backlight()
        pad.hid.close()


if __name__ == '__main__':
    main()
