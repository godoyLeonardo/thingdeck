"""A small iCalendar (RFC 5545) reader for the agenda, with no dependencies (the device
has plain Python 3.7). It handles what calendar feeds such as Google Calendar's
"secret address in iCal format" use: single and all-day events, repeats (RRULE with
COUNT/UNTIL/INTERVAL/BYDAY/BYMONTHDAY/BYMONTH/BYSETPOS, EXDATE, RDATE), moved or
cancelled occurrences (RECURRENCE-ID) and the time zones the file defines (VTIMEZONE).

    cal = Calendar(text)
    cal.between(start_utc, end_utc, local_offset)   # naive UTC datetimes, timedelta
"""
import calendar as _calendar
import datetime as dt
import re

WEEKDAYS = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')
MEETING_URL = re.compile(
    r'https://(?:meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}'
    r'|(?:[a-z0-9-]+\.)?zoom\.us/(?:j|my|w)/[^\s"<>\\]+'
    r'|teams\.microsoft\.com/l/meetup-join/[^\s"<>\\]+'
    r'|teams\.live\.com/meet/[^\s"<>\\]+)', re.I)
MAX_PERIODS = 5000      # repeat periods to walk through for one event in one query


# ------------------------------------------------------------------ parsing

def parse_line(line):
    """'NAME;P=V;Q="a:b":value' -> ('NAME', {'P': 'V', 'Q': 'a:b'}, 'value')."""
    i = line.find(':')
    if i < 0:
        return None
    if ';' not in line[:i]:          # most lines: no parameters
        return line[:i].upper(), {}, line[i + 1:]
    if '"' in line[:i]:              # a quoted parameter may hold a colon
        quoted = False
        for i, ch in enumerate(line):
            if ch == '"':
                quoted = not quoted
            elif ch == ':' and not quoted:
                break
        else:
            return None
    parts = re.findall(r'(?:[^;"]|"[^"]*")+', line[:i])
    if not parts:
        return None
    params = {}
    for p in parts[1:]:
        key, _, value = p.partition('=')
        params[key.upper()] = value.replace('"', '')
    return parts[0].upper(), params, line[i + 1:]


def components(text):
    """The component tree: {'name', 'props': [(name, params, value)], 'children'}."""
    root = {'name': 'ROOT', 'props': [], 'children': []}
    stack = [root]
    for line in re.sub(r'\r?\n[ \t]', '', text).splitlines():     # unfold long lines
        parsed = parse_line(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name == 'BEGIN':
            child = {'name': value.strip().upper(), 'props': [], 'children': []}
            stack[-1]['children'].append(child)
            stack.append(child)
        elif name == 'END':
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1]['props'].append((name, params, value))
    return root


def unescape(value):
    return re.sub(r'\\([\\;,nN])', lambda m: '\n' if m.group(1) in 'nN' else m.group(1), value)


def parse_time(value, params):
    """-> (naive datetime, zone) where zone is 'utc', 'date' (all day), a TZID or None (floating)."""
    v = value.strip()       # by hand: strptime is slow on the device
    if params.get('VALUE') == 'DATE' or len(v) == 8:
        return dt.datetime(int(v[0:4]), int(v[4:6]), int(v[6:8])), 'date'
    return (dt.datetime(int(v[0:4]), int(v[4:6]), int(v[6:8]), int(v[9:11]), int(v[11:13]), int(v[13:15])),
            'utc' if v.endswith('Z') else params.get('TZID'))


def parse_times(value, params):
    return [parse_time(v, params) for v in value.split(',') if v.strip()]


def parse_offset(value):
    """'+0130' / '-0300' -> timedelta."""
    m = re.match(r'([+-])(\d\d)(\d\d)(\d\d)?', value.strip())
    if not m:
        return dt.timedelta(0)
    delta = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)), seconds=int(m.group(4) or 0))
    return -delta if m.group(1) == '-' else delta


def parse_duration(value):
    """'PT1H30M', 'P1D', '-PT15M' -> timedelta."""
    m = re.match(r'([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?', value.strip())
    if not m:
        return dt.timedelta(0)
    weeks, days, hours, minutes, seconds = (int(x or 0) for x in m.groups()[1:])
    delta = dt.timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return -delta if m.group(1) == '-' else delta


def parse_rule(value):
    rule = {}
    for part in value.split(';'):
        key, _, v = part.partition('=')
        rule[key.strip().upper()] = v.strip().upper()
    return rule


# ------------------------------------------------------------------ repeats

def month_days(year, month, byday, bymonthday, default_day):
    last = _calendar.monthrange(year, month)[1]
    days = set()
    for md in bymonthday:
        d = md if md > 0 else last + 1 + md
        if 1 <= d <= last:
            days.add(d)
    for nth, weekday in byday:
        matching = [d for d in range(1, last + 1) if dt.date(year, month, d).weekday() == weekday]
        if nth == 0:
            days.update(matching)
        elif -len(matching) <= (nth - 1 if nth > 0 else nth) < len(matching):
            days.add(matching[nth - 1 if nth > 0 else nth])
    if not bymonthday and not byday and default_day <= last:
        days.add(default_day)
    return [dt.date(year, month, d) for d in sorted(days)]


def period_dates(freq, first, k, interval, byday, bymonthday, bymonth, wkst):
    """Candidate dates in the k-th repeat period, sorted."""
    if freq == 'DAILY':
        d = first + dt.timedelta(days=k * interval)
        if (bymonth and d.month not in bymonth) or (byday and d.weekday() not in [w for _, w in byday]) \
                or (bymonthday and d.day not in bymonthday):
            return []
        return [d]
    if freq == 'WEEKLY':
        week = first - dt.timedelta(days=(first.weekday() - wkst) % 7) + dt.timedelta(weeks=k * interval)
        weekdays = sorted({w for _, w in byday} or {first.weekday()}, key=lambda w: (w - wkst) % 7)
        return [d for d in (week + dt.timedelta(days=(w - wkst) % 7) for w in weekdays)
                if not bymonth or d.month in bymonth]
    if freq == 'MONTHLY':
        m0 = first.month - 1 + k * interval
        year, month = first.year + m0 // 12, m0 % 12 + 1
        if bymonth and month not in bymonth:
            return []
        return month_days(year, month, byday, bymonthday, first.day)
    if freq == 'YEARLY':
        year = first.year + k * interval
        dates = []
        for month in bymonth or [first.month]:
            dates += month_days(year, month, byday, bymonthday, first.day)
        return dates
    return None


def period_index(freq, first, day, interval):
    """The repeat period that contains `day` (an estimate from below)."""
    if day <= first:
        return 0
    if freq == 'DAILY':
        return (day - first).days // interval
    if freq == 'WEEKLY':
        return (day - first).days // 7 // interval
    if freq == 'MONTHLY':
        return ((day.year - first.year) * 12 + day.month - first.month) // interval
    if freq == 'YEARLY':
        return (day.year - first.year) // interval
    return 0


def expand(rule, start, lo, hi, to_utc=lambda d: d):
    """Start times (naive, in the event's own zone) of a repeating event within [lo, hi]."""
    freq = rule.get('FREQ')
    interval = max(1, int(rule.get('INTERVAL') or 1))
    count = int(rule['COUNT']) if rule.get('COUNT', '').isdigit() else None
    until = None
    if rule.get('UNTIL'):
        value, zone = parse_time(rule['UNTIL'], {})
        until = (zone == 'utc', value + dt.timedelta(days=1, seconds=-1) if zone == 'date' else value)
    byday = [(int(m.group(1) or 0), WEEKDAYS.index(m.group(2)))
             for m in re.finditer(r'([+-]?\d*)(MO|TU|WE|TH|FR|SA|SU)', rule.get('BYDAY', ''))]
    ints = lambda key: [int(x) for x in rule.get(key, '').split(',') if re.match(r'^[+-]?\d+$', x)]
    bymonthday, bymonth, bysetpos = ints('BYMONTHDAY'), ints('BYMONTH'), ints('BYSETPOS')
    wkst = WEEKDAYS.index(rule['WKST']) if rule.get('WKST') in WEEKDAYS else 0
    first, at = start.date(), start.time()
    # Without COUNT, periods before the window can be skipped; with it they must be counted.
    k = 0 if count is not None else max(0, period_index(freq, first, lo.date(), interval) - 1)
    found, n = [], 0
    for k in range(k, k + MAX_PERIODS):
        dates = period_dates(freq, first, k, interval, byday, bymonthday, bymonth, wkst)
        if dates is None:
            break
        if bysetpos:
            dates = sorted({dates[p - 1 if p > 0 else p] for p in bysetpos
                            if -len(dates) <= (p - 1 if p > 0 else p) < len(dates)})
        for d in dates:
            occurrence = dt.datetime.combine(d, at)
            if occurrence < start:
                continue
            if until and (to_utc(occurrence) if until[0] else occurrence) > until[1]:
                return found
            n += 1
            if (count is not None and n > count) or occurrence > hi:
                return found
            if occurrence >= lo:
                found.append(occurrence)
    return found


class Zone:
    """A VTIMEZONE: its STANDARD/DAYLIGHT observances and when each starts."""

    def __init__(self, comp):
        self.observances = []      # (first onset, offset from, offset to, rule, extra onsets)
        for child in comp['children']:
            if child['name'] not in ('STANDARD', 'DAYLIGHT'):
                continue
            props = {name: (params, value) for name, params, value in child['props']}
            if 'DTSTART' not in props:
                continue
            onset = parse_time(props['DTSTART'][1], {})[0]
            rule = parse_rule(props['RRULE'][1]) if 'RRULE' in props else None
            extra = [t for name, params, value in child['props'] if name == 'RDATE'
                     for t, _ in parse_times(value, params)]
            self.observances.append((onset, parse_offset(props.get('TZOFFSETFROM', ({}, '+0000'))[1]),
                                     parse_offset(props.get('TZOFFSETTO', ({}, '+0000'))[1]), rule, extra))
        self.cache = {}

    def offset(self, local):
        """UTC offset at a local wall-clock time: that of the latest observance onset before it."""
        key = (local.year, local.month, local.day, local.hour)
        if key not in self.cache:
            best = None
            for onset, before, after, rule, extra in self.observances:
                onsets = [onset] + extra
                if rule:
                    onsets += expand(rule, onset, dt.datetime(local.year - 1, 1, 1), local)
                for o in onsets:
                    if o <= local and (best is None or o > best[0]):
                        best = (o, after)
            if best is None:   # before the first onset: the earliest observance's "from"
                best = (None, min(self.observances, key=lambda o: o[0])[1] if self.observances else dt.timedelta(0))
            if len(self.cache) > 5000:
                self.cache.clear()
            self.cache[key] = best[1]
        return self.cache[key]


# ------------------------------------------------------------------ events

class Calendar:
    def __init__(self, text, since=None):
        """since (naive UTC): one-off events that ended a day before it are skipped."""
        self.since = since
        self.zones = {}
        self.events = []
        self.moved = set()     # (uid, original start in UTC) of occurrences that have their own VEVENT
        root = components(text)
        cals = [c for c in root['children'] if c['name'] == 'VCALENDAR'] or [root]
        for cal in cals:
            for child in cal['children']:
                if child['name'] == 'VTIMEZONE':
                    tzid = next((v.strip() for n, p, v in child['props'] if n == 'TZID'), None)
                    if tzid:
                        self.zones[tzid] = Zone(child)
        for index, cal in enumerate(cals):
            # A Google Calendar feed is named after its owner's address, which marks their RSVP.
            owner = next((v.strip().lower() for n, p, v in cal['props'] if n == 'X-WR-CALNAME'), '')
            for child in cal['children']:
                if child['name'] == 'VEVENT':
                    event = self.read_event(child, owner, index)
                    if event:
                        self.events.append(event)

    def read_event(self, comp, owner, index):
        props, multi = {}, {}
        for name, params, value in comp['props']:
            props.setdefault(name, (params, value))
            multi.setdefault(name, []).append((params, value))
        if 'DTSTART' not in props or props.get('STATUS', ({}, ''))[1].strip().upper() == 'CANCELLED':
            if 'RECURRENCE-ID' in props and 'UID' in props:     # a cancelled occurrence: drop it
                self.moved.add((props['UID'][1].strip(), self.utc(*parse_time(props['RECURRENCE-ID'][1],
                                                                             props['RECURRENCE-ID'][0]))))
            return None
        for params, value in multi.get('ATTENDEE', []):
            if owner and value.strip().lower() == 'mailto:' + owner and params.get('PARTSTAT') == 'DECLINED':
                return None
        uid = props.get('UID', ({}, ''))[1].strip()
        if 'RECURRENCE-ID' in props:
            self.moved.add((uid, self.utc(*parse_time(props['RECURRENCE-ID'][1], props['RECURRENCE-ID'][0]))))
        start, zone = parse_time(props['DTSTART'][1], props['DTSTART'][0])
        if 'DTEND' in props:
            end = parse_time(props['DTEND'][1], props['DTEND'][0])[0]
        elif 'DURATION' in props:
            end = start + parse_duration(props['DURATION'][1])
        else:
            end = start + (dt.timedelta(days=1) if zone == 'date' else dt.timedelta(0))
        if self.since and 'RRULE' not in props and 'RDATE' not in props and end + dt.timedelta(days=1) < self.since:
            return None     # long over (a day covers any UTC offset)
        text = ' '.join(unescape(props[k][1]) for k in ('X-GOOGLE-CONFERENCE', 'LOCATION', 'DESCRIPTION', 'URL')
                        if k in props)
        link = MEETING_URL.search(text)
        event = {
            'uid': uid, 'start': start, 'zone': zone, 'length': max(end - start, dt.timedelta(0)),
            'title': unescape(props.get('SUMMARY', ({}, ''))[1]).strip() or '(No title)',
            'where': unescape(props.get('LOCATION', ({}, ''))[1]).strip().split('\n')[0],
            'link': link.group(0) if link else None, 'cal': index,
            'rule': parse_rule(props['RRULE'][1]) if 'RRULE' in props else None,
            'exdates': [t for params, value in multi.get('EXDATE', []) for t in parse_times(value, params)],
            'rdates': [t for params, value in multi.get('RDATE', []) for t in parse_times(value, params)
                       if t[1] != 'date' or zone == 'date'],
        }
        return event

    def utc(self, local, zone, local_offset=dt.timedelta(0)):
        """A time in its zone -> naive UTC. All-day and floating times use the computer's offset."""
        if zone == 'utc':
            return local
        if zone in self.zones:
            return local - self.zones[zone].offset(local)
        return local - local_offset

    def between(self, lo, hi, local_offset=dt.timedelta(0)):
        """Occurrences overlapping [lo, hi] (naive UTC), sorted by start:
        dicts with start/end (naive UTC), all_day, title, where, link, cal, uid."""
        out = []
        for ev in self.events:
            to_utc = lambda t, zone=ev['zone']: self.utc(t, zone, local_offset)
            if ev['rule']:
                # The window in the event's own zone, widened by a day for offsets and length.
                margin = dt.timedelta(days=1)
                starts = expand(ev['rule'], ev['start'], lo - ev['length'] - margin, hi + margin, to_utc)
                starts += [t for t, zone in ev['rdates'] if t >= ev['start']]
                skip = {self.utc(t, zone, local_offset) for t, zone in ev['exdates']}
            else:
                starts, skip = [ev['start']], set()
            for start in starts:
                s = to_utc(start)
                if s in skip or (ev['rule'] and (ev['uid'], self.utc(start, ev['zone'])) in self.moved):
                    continue
                e = to_utc(start + ev['length'])
                if e > lo and s < hi or (s == e and lo <= s < hi):
                    out.append({'start': s, 'end': e, 'all_day': ev['zone'] == 'date', 'title': ev['title'],
                                'where': ev['where'], 'link': ev['link'], 'cal': ev['cal'], 'uid': ev['uid']})
        out.sort(key=lambda o: (o['start'], not o['all_day'], o['title']))
        return out
