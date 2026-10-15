#!/usr/bin/env python3
"""Car Thing macro pad.

Shows up on the host as a USB HID keyboard + media keys + mouse wheel (built in
user space on FunctionFS, because this kernel has no f_hid). The dial, the
hardware buttons and a touch UI in the kiosk browser send the reports.
"""
import fcntl
import json
import os
import queue
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'config.json')
STATE_PATH = os.path.join(BASE, 'state.json')
GADGET_SH = os.path.join(BASE, 'gadget.sh')
FFS_DIR = '/dev/usb-ffs/hid'
WEBAPP_DIR = '/usr/share/qt-superbird-app/webapp'
BACKLIGHT = '/sys/class/backlight/aml-bl/brightness'
HTTP_PORT = 8765


def log(*args):
    print(time.strftime('%H:%M:%S'), *args, flush=True)


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
    """Latin American (Windows 'Latin American', xkb 'latam'). Identical on both OSes."""
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


# ------------------------------------------------------------------ the pad

class Pad:
    def __init__(self):
        self.ui = Broadcaster()
        self.hid = Hid(lambda status: self.on_usb_status(status))
        self.net = Net(self.publish_state)
        self.config = {}
        self.config_mtime = 0
        self.page = 0
        self.dial = 0
        self.mode = 'starting'   # starting | ok | safe | error
        self.message = ''
        self.os_detected = None
        # Device settings changed from the Settings tab; kept in state.json.
        # None means "use config.json".
        self.settings = {'os_mode': 'auto', 'layout': None, 'dial_invert': None,
                         'brightness_auto': True, 'brightness': 180, 'clock_24h': True}
        self.lock = threading.RLock()
        self.hid.on_report_request = self.detect_os
        self.load_saved()
        self.load_config()

    # -- host OS: detected from how the host asks for the HID report descriptor
    # (Windows asks for more bytes than the descriptor has, Linux asks for the exact size)
    def detect_os(self, length):
        detected = 'windows' if length > len(REPORT_DESCRIPTOR) else 'linux'
        log('report descriptor requested with wLength=%d -> %s' % (length, detected))
        if detected != self.os_detected:
            self.os_detected = detected
            self.publish_state()

    def host_os(self):
        if self.settings['os_mode'] != 'auto':
            return self.settings['os_mode']
        return self.os_detected or self.config.get('default_os', 'windows')

    def layout_name(self):
        return (self.settings['layout'] or self.config.get('layout_' + self.host_os())
                or self.config.get('layout', 'us'))

    def dial_invert(self):
        if self.settings['dial_invert'] is not None:
            return self.settings['dial_invert']
        return bool(self.config.get('hardware', {}).get('dial_invert'))

    def set_os(self, ref):
        order = ['auto', 'windows', 'linux']
        if ref == 'next':
            ref = order[(order.index(self.settings['os_mode']) + 1) % 3]
        self.set_setting('os_mode', ref)

    def set_setting(self, key, value):
        valid = {
            'os_mode': lambda v: v in ('auto', 'windows', 'linux'),
            'layout': lambda v: v is None or v in LAYOUTS,
            'dial_invert': lambda v: v is None or isinstance(v, bool),
            'clock_24h': lambda v: isinstance(v, bool),
            'brightness_auto': lambda v: isinstance(v, bool),
            'brightness': lambda v: isinstance(v, int) and 5 <= v <= 255,
        }
        if key not in valid or not valid[key](value):
            raise ValueError('bad setting %s=%r' % (key, value))
        self.settings[key] = value
        if key.startswith('brightness'):
            self.apply_brightness()
        self.save()
        self.publish_state()

    def apply_brightness(self):
        # The stock sp-als-backlight service drives the backlight from the light
        # sensor; manual mode stops it. supervisorctl is slow, so only on a switch.
        auto = self.settings['brightness_auto']
        if auto != getattr(self, '_als_running', None):
            subprocess.call(['supervisorctl', 'start' if auto else 'stop', 'backlight'], stdout=subprocess.DEVNULL)
            self._als_running = auto
        if auto:
            return
        try:
            with open(BACKLIGHT, 'w') as f:
                f.write(str(260 - self.settings['brightness']))   # panel PWM is inverted: 5 = brightest
        except OSError as e:
            log('brightness:', e)

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
            'net': self.net.status(),
        }

    def variant(self, a):
        """An action may carry per-OS overrides: {"key": "win+tab", "linux": {"key": "win"}}."""
        override = a.get(self.host_os()) if isinstance(a, dict) else None
        if not isinstance(override, dict):
            return a
        merged = {k: a[k] for k in ('label', 'icon', 'color') if k in a}
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
            # POSIX TZ rule: the device has no zoneinfo database, and its browser has no Intl.
            if cfg.get('tz'):
                os.environ['TZ'] = cfg['tz']
                time.tzset()
            self.page = min(self.page, max(0, len(self.pages()) - 1))
            self.dial = min(self.dial, max(0, len(self.dials()) - 1))
        log('config loaded: %d pages, %d dial modes' % (len(self.pages()), len(self.dials())))
        return True

    def pages(self):
        return self.config.get('pages', [])

    def dials(self):
        return self.config.get('dial_modes', [])

    def state(self):
        return {'page': self.page, 'dial': self.dial, 'usb': self.hid.status,
                'mode': self.mode, 'message': self.message,
                'os': self.host_os(), 'os_mode': self.settings['os_mode'], 'os_detected': self.os_detected,
                'settings': self.settings, 'layout': self.layout_name(), 'dial_invert': self.dial_invert(),
                'net': self.net.status(), 'timezone': self.config.get('timezone', 'UTC'),
                'utc_offset': time.localtime().tm_gmtoff}

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
        try:
            self._run(action)
        except Exception as e:  # a bad config entry must never kill the input loop
            log('action failed:', action, e)
            self.toast('Action error: %s' % e, 'fail')

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
        elif 'notify' in a:
            self.toast(a['notify'], a.get('level', 'info'))
        else:
            raise ValueError('no action in %r' % (a,))

    def set_page(self, ref):
        with self.lock:
            n = len(self.pages())
            if not n:
                return
            if ref == 'next':
                self.page = (self.page + 1) % n
            elif ref == 'prev':
                self.page = (self.page - 1) % n
            else:
                idx = self._resolve(self.pages(), ref)
                if idx is not None:
                    self.page = idx
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

    # -- standby: the computer is asleep or off, the screen shows a clock.
    # The Car Thing cannot wake the computer: with the host asleep the Amlogic USB PHY is
    # in low-power mode and never drives the resume signal, and asking the controller to
    # send it locked it up until a power cycle. So nothing touches the controller here.
    def standby(self):
        return self.mode == 'ok' and self.hid.status != 'connected'

    def on_usb_status(self, status):
        # While the host sleeps, keep the USB network quiet.
        if status == 'suspended':
            self.net.suspend()
        elif status == 'connected':
            self.net.resume()
        self.publish_state()

    # -- triggers
    def tap_tile(self, page, index):
        pages = self.pages()
        if 0 <= page < len(pages):
            tiles = pages[page].get('tiles', [])
            if 0 <= index < len(tiles) and tiles[index]:
                self.flash('tile', index, tiles[index].get('label'))
                self.run(tiles[index])

    def press_preset(self, index):
        if self.standby():
            return
        preset = self.presets()[index] if 0 <= index < 4 else None
        if preset:
            self.flash('preset', index, preset.get('label'))
            self.run(preset)

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


# ------------------------------------------------------- USB network + clock

NTP_PEERS = ['pool.ntp.org', 'time.cloudflare.com', '162.159.200.1']   # last one needs no DNS
VALID_TIME = 1735689600       # 2025-01-01: anything earlier means the clock was never set
RESYNC_SECONDS = 3600


class Net:
    """DHCP on the RNDIS interface (the PC shares its internet connection), then NTP."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.ip = None
        self.gateway = None
        self.synced_at = None      # monotonic time of the last good sync
        self.sync_error = None
        self.dhcp = None
        self.started = False
        self.paused = False

    def suspend(self):
        """Host went to sleep: take usb0 down so no packet goes out and wakes it."""
        if self.started and not self.paused and os.path.exists('/sys/class/net/usb0'):
            subprocess.call(['ip', 'link', 'set', 'usb0', 'down'])
            self.paused = True
            log('usb network paused while the host sleeps')

    def resume(self):
        if self.paused:
            self.paused = False
            subprocess.call(['ip', 'link', 'set', 'usb0', 'up'])
            if self.dhcp and self.dhcp.poll() is None:
                self.dhcp.send_signal(signal.SIGUSR1)    # renew: link down dropped the routes
            log('usb network resumed')

    def status(self):
        return {'ip': self.ip, 'gateway': self.gateway, 'time_valid': time.time() > VALID_TIME,
                'synced_ago': None if self.synced_at is None else int(time.monotonic() - self.synced_at),
                'sync_error': self.sync_error}

    def start(self):
        self.started = True
        threading.Thread(target=self._loop, name='net', daemon=True).start()

    def _addresses(self):
        out = subprocess.run(['ip', '-4', 'addr', 'show', 'usb0'], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True).stdout
        ip = None
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] == 'inet':
                ip = parts[1].split('/')[0]
        route = subprocess.run(['ip', 'route', 'show', 'default'], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, universal_newlines=True).stdout.split()
        gateway = route[route.index('via') + 1] if 'via' in route else None
        return ip, gateway

    def _loop(self):
        while not os.path.exists('/sys/class/net/usb0'):
            time.sleep(2)
        subprocess.call(['ifconfig', 'usb0', 'up'])
        # A previous run's udhcpc survives a service restart; two clients fight over usb0.
        subprocess.call(['killall', 'udhcpc'], stderr=subprocess.DEVNULL)
        dhcp_started = 0
        while True:
            if self.paused:
                time.sleep(2)
                continue
            # udhcpc can sit in a back-off and miss a DHCP server that appears later
            # (sharing turned on, laptop moved to another PC): restart it while unleased.
            if self.ip is None and time.monotonic() - dhcp_started > 30:
                if self.dhcp:
                    self.dhcp.kill()
                    self.dhcp.wait()
                self.dhcp = subprocess.Popen(['udhcpc', '-f', '-i', 'usb0', '-s', '/etc/udhcpc/default.script',
                                              '-x', 'hostname:carthing', '-t', '4', '-T', '3', '-A', '5'],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                dhcp_started = time.monotonic()
            ip, gateway = self._addresses()
            if (ip, gateway) != (self.ip, self.gateway):
                self.ip, self.gateway = ip, gateway
                log('usb network: ip=%s gateway=%s' % (ip, gateway))
                if ip is None:
                    self.synced_at = None
                self.on_change()
            due = self.synced_at is None or time.monotonic() - self.synced_at > RESYNC_SECONDS
            if gateway and due:
                self._sync()
            time.sleep(5 if self.synced_at is None else 30)

    def _sync(self):
        cmd = ['ntpd', '-n', '-q']
        for peer in NTP_PEERS:
            cmd += ['-p', peer]
        try:
            rc = subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
        except subprocess.TimeoutExpired:
            rc = -1
        if rc == 0 and time.time() > VALID_TIME:
            self.synced_at = time.monotonic()
            self.sync_error = None
            log('clock synced:', time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()))
        else:
            self.sync_error = 'no reply from time servers (is internet sharing on?)'
            log('ntp failed, rc', rc)
        self.on_change()


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
    down = {}   # code -> (deadline, hold fired)

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
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        for k, v in (extra or {}).items():
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
        if not self._allowed(path):
            return self._send(403, {'error': 'only /api/ping and /api/notify are reachable over the network'})
        if path in ('/', '/index.html'):
            self._file(os.path.join(BASE, 'web', 'index.html'), 'text/html; charset=utf-8')
        elif path.startswith('/fonts/') and '..' not in path:
            self._file(os.path.join(WEBAPP_DIR, path.lstrip('/')), 'font/ttf')
        elif path == '/api/ping':
            self._send(200, {'ok': True})
        elif path == '/api/info':
            self._send(200, pad.info())
        elif path == '/api/state':
            self._send(200, {'config': pad.config, 'state': pad.state(), 'presets': pad.presets()})
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

    def _allowed(self, path):
        """Over the USB network only notifications are accepted; keystrokes stay local to the UI."""
        return self.client_address[0] == '127.0.0.1' or path in ('/api/ping', '/api/notify')

    def do_POST(self):
        pad = self.pad
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._send(400, {'error': 'expected a JSON object'})
        path = self.path.split('?')[0]
        if not self._allowed(path):
            return self._send(403, {'error': 'only /api/ping and /api/notify are reachable over the network'})
        if path == '/api/tap':
            threading.Thread(target=pad.tap_tile, args=(int(body.get('page', -1)), int(body.get('index', -1))),
                             daemon=True).start()
        elif path == '/api/preset':
            pad.press_preset(int(body.get('index', -1)))
        elif path == '/api/page':
            pad.set_page(body.get('page', 'next'))
        elif path == '/api/dial':
            pad.set_dial(body.get('dial', 'next'))
        elif path == '/api/notify':
            pad.toast(str(body.get('text', ''))[:200], body.get('level', 'info'))
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
    server = ThreadingHTTPServer(('0.0.0.0', HTTP_PORT), Handler)
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


SUPERBIRD_LOG = '/var/log/superbird.info'
BOOT_OK_MARKER = b'Boot marked as successful'


def wait_for_boot_marked_successful(timeout=90):
    """The bootloader keeps A/B slots: every boot spends one "try" of the active slot
    until something marks the boot successful, and after 7 unmarked boots it falls back
    to the other slot. On this system qt-superbird-app does the marking, ~24 s after it
    starts. Stopping it earlier made the device switch slots, losing the boot hook.
    So let it run until its log says so (or a timeout) before stopping it."""
    def marks():
        try:
            with open(SUPERBIRD_LOG, 'rb') as f:
                return f.read().count(BOOT_OK_MARKER)
        except OSError:
            return 0
    with open('/proc/uptime') as f:
        uptime = float(f.read().split()[0])
    if uptime > 180:
        return            # booted long ago (service restart): already marked
    before = marks()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        if marks() > before:
            log('boot slot marked successful by the stock app')
            return
    log('no boot-success mark after %d s; stopping the stock app anyway' % timeout)


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
    if not pad.settings['brightness_auto']:
        pad.apply_brightness()
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
                use_net = pad.config.get('usb_network', True)
                gadget('bind-hid', 'net' if use_net else '')
                pad.mode = 'ok'
                threading.Thread(target=usb_watchdog, args=(pad,), name='usb-watchdog', daemon=True).start()
                if use_net:
                    pad.net.start()
            except Exception as e:
                log('HID setup failed:', e)
                pad.hid.close()
                subprocess.call(['sh', GADGET_SH, 'adb-only'])
                pad.mode, pad.message = 'error', 'USB HID setup failed: %s' % e
        pad.publish_state()
        threading.Thread(target=input_loop, args=(pad, keys_path, rotary_path), name='input', daemon=True).start()
        offset = time.localtime().tm_gmtoff
        while True:
            time.sleep(2)
            if pad.load_config():
                pad.ui.publish('config', {})
            if time.localtime().tm_gmtoff != offset:     # DST switch: the clock needs the new offset
                offset = time.localtime().tm_gmtoff
                pad.publish_state()
    finally:
        pad.hid.close()


if __name__ == '__main__':
    main()
