#!/usr/bin/env python3
"""Talk to the kiosk browser through its DevTools port (2222), on the device.

  python3 cdp.py screenshot /tmp/screen.png   save a PNG of the screen
  python3 cdp.py eval "document.title"        run JavaScript in the page, print the result
  python3 cdp.py reload                       reload the page
"""
import base64
import json
import os
import socket
import struct
import sys
import urllib.request


class DevTools:
    def __init__(self, port=2222):
        pages = json.load(urllib.request.urlopen('http://127.0.0.1:%d/json' % port))
        ws_url = next(p['webSocketDebuggerUrl'] for p in pages if p['type'] == 'page')
        path = ws_url.split('127.0.0.1:%d' % port, 1)[1]
        self.sock = socket.create_connection(('127.0.0.1', port))
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n' % (path, port, key)).encode())
        self.buf = b''
        while b'\r\n\r\n' not in self.buf:
            self.buf += self.sock.recv(4096)
        self.buf = self.buf.split(b'\r\n\r\n', 1)[1]
        self.next_id = 0

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError('devtools closed the connection')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, payload):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = bytes([0x81, 0x80 | n])
        elif n < 65536:
            head = bytes([0x81, 0x80 | 126]) + struct.pack('>H', n)
        else:
            head = bytes([0x81, 0x80 | 127]) + struct.pack('>Q', n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def call(self, method, **params):
        self.next_id += 1
        self._send_frame(json.dumps({'id': self.next_id, 'method': method, 'params': params}).encode())
        message = b''
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack('>H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._read(8))[0]
            message += self._read(n)
            if b0 & 0x80:
                reply = json.loads(message.decode())
                message = b''
                if reply.get('id') == self.next_id:
                    if 'error' in reply:
                        raise RuntimeError(reply['error'])
                    return reply.get('result', {})


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    dt = DevTools()
    cmd = sys.argv[1]
    if cmd == 'screenshot':
        out = sys.argv[2] if len(sys.argv) > 2 else '/tmp/screen.png'
        data = dt.call('Page.captureScreenshot', format='png')['data']
        with open(out, 'wb') as f:
            f.write(base64.b64decode(data))
        print('saved', out)
    elif cmd == 'eval':
        result = dt.call('Runtime.evaluate', expression=sys.argv[2], returnByValue=True)
        if 'exceptionDetails' in result:
            print('exception:', result['exceptionDetails'].get('exception', {}).get('description'))
        else:
            print(json.dumps(result['result'].get('value')))
    elif cmd == 'reload':
        dt.call('Page.reload', ignoreCache=True)
        print('reloaded')
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
