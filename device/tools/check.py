#!/usr/bin/env python3
"""Smoke test after a deploy, on the device: is the service up, the agent current, does each
live page get its data, and does every page draw without a JavaScript error.

  python3 check.py            prints a report; the last line is CHECK_OK or CHECK_FAILED
                              (adb doesn't pass exit codes back, so deploy.sh reads that line)

FAIL: the service or the page doesn't answer, an outdated agent, a page that throws.
WARN: things that can be outside the pad (no agent, a server that doesn't answer).
Screenshots of each page go to /tmp/thingdeck-check/.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import DevTools      # noqa: E402

API = 'http://127.0.0.1:8765/api/'
SHOTS = '/tmp/thingdeck-check'
# What each live widget's data is called in /api/state.
FEEDS = {'lights': 'hue', 'agenda': 'agenda', 'github': 'github', 'plex': 'plex', 'truenas': 'truenas'}
problems = {'FAIL': [], 'WARN': []}


def report(level, text):
    print('%-4s %s' % (level, text))
    if level in problems:
        problems[level].append(text)


def state(wait=60):
    for _ in range(wait // 2):
        try:
            return json.load(urllib.request.urlopen(API + 'state', timeout=5))
        except (OSError, ValueError):
            time.sleep(2)
    return None


def post(path, body):
    urllib.request.urlopen(urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                                  headers={'Content-Type': 'application/json'}), timeout=5)


def js(code):
    reply = DevTools().call('Runtime.evaluate', expression=code, returnByValue=True)
    if 'exceptionDetails' in reply:
        raise RuntimeError(reply['exceptionDetails'].get('exception', {}).get('description') or 'exception')
    return reply['result'].get('value')


def main():
    s = state()
    if not s:
        report('FAIL', 'the service does not answer on 127.0.0.1:8765')
        return
    st = s['state']
    report('OK' if st.get('mode') == 'ok' else 'FAIL', 'service: mode %s%s' % (st.get('mode'), ': ' + st['message'] if st.get('message') else ''))
    report('OK' if st.get('usb') == 'connected' else 'WARN', 'USB: %s' % st.get('usb'))

    # The agent: its heartbeat sets the clock; give it a minute after a restart.
    for _ in range(30):
        if st.get('clock') and st.get('agent'):
            break
        time.sleep(2)
        st = state()['state']
    agent = st.get('agent')
    if not agent:
        report('WARN', 'agent: not connected (the live pages stay empty)')
    elif agent.get('outdated'):
        report('FAIL', 'agent: %s %s is out of date (expected %s): run ./deploy.sh all' % (agent['os'], agent['version'], agent.get('expected')))
    else:
        report('OK', 'agent: %s %s, up to date' % (agent['os'], agent['version']))

    # Each live page's data: "ok", "none" (not set up here) or an error. After a restart the
    # agent resends everything at once, but GitHub and the calendars take a few seconds.
    pages = [p for p in s['config'].get('pages', []) if p.get('name') not in (st.get('hidden') or [])]
    used = [FEEDS[w] for p in s['config'].get('pages', []) for w in p.get('widgets') or [] if w in FEEDS]
    if agent:
        for _ in range(45):
            s = state()
            if all((s.get(k) or {}).get('state') not in (None, 'unknown') for k in used + ['weather']):
                break
            time.sleep(2)
    for key in used:
        data = s.get(key) or {}
        status = data.get('state') or 'unknown'
        if status == 'ok' and data.get('error'):
            report('WARN', '%s: %s' % (key, data['error']))
        elif status == 'ok':
            report('OK', '%s: ok' % key)
        elif status == 'none':
            report('OK', '%s: not set up on this computer' % key)
        else:
            report('WARN', '%s: %s%s' % (key, status, ': ' + data['error'] if data.get('error') else ''))

    # The weather under the large clocks (not a page of its own).
    weather = s.get('weather') or {}
    if weather.get('state') == 'ok':
        report('OK', 'weather: %s°%s%s' % (weather.get('temp'), weather.get('unit') or '', ' in ' + weather['place'] if weather.get('place') else ''))
    elif weather.get('state') == 'none':
        report('OK', 'weather: not set up on this computer')
    elif agent:
        report('WARN', 'weather: %s%s' % (weather.get('state') or 'unknown', ': ' + weather['error'] if weather.get('error') else ''))

    # The page: connected to the service, and every tab drawn without an error.
    try:
        js('window.__checkErrors = []; window.addEventListener("error", function (e) {'
           ' window.__checkErrors.push(e.message + " (line " + e.lineno + ")"); }); 1')
        report('OK' if not js('document.getElementById("offline").className') else 'FAIL',
               'page: connected to the service')
        os.makedirs(SHOTS, exist_ok=True)
        start = st.get('page', 0)
        for i, page in enumerate(pages):
            post('page', {'page': i})
            time.sleep(2.5)
            try:
                js('render(); 1')
                report('OK', 'page %d %s: drawn' % (i + 1, page.get('name')))
            except RuntimeError as e:
                report('FAIL', 'page %d %s: %s' % (i + 1, page.get('name'), e))
            subprocess.run(['python3', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cdp.py'), 'screenshot',
                            os.path.join(SHOTS, '%d-%s.png' % (i + 1, page.get('name', 'page').lower()))], stdout=subprocess.DEVNULL)
        post('page', {'page': start})
        for error in js('window.__checkErrors') or []:
            report('FAIL', 'page error: %s' % error)
    except (OSError, ValueError, RuntimeError, StopIteration, KeyError) as e:
        report('FAIL', 'page: the browser does not answer on its DevTools port (%s)' % e)


if __name__ == '__main__':
    main()
    print('%d failed, %d warnings; screenshots in %s' % (len(problems['FAIL']), len(problems['WARN']), SHOTS))
    print('CHECK_FAILED' if problems['FAIL'] else 'CHECK_OK')
