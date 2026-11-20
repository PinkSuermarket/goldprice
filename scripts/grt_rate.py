#!/usr/bin/env python3
"""Keep grt-rate.json in step with GRT Jewellers' published gold board rate.

Runs in GitHub Actions every hour from 9 AM to 9 PM IST (see .github/workflows/grt-rate.yml).
Standard library only. grt-rate.json is read by gold-signal-deck.html; every reading is
also added as a row to grt-history.csv, so the hourly record builds up day by day.

Sources, in order:
  1. grtjewels.com home page (rate text such as "GOLD - 22KT - 1 g - Rs 11980")
  2. livechennai.com GRT Chennai table (one dated row per day)

Each run stores that hour's reading in "hourly". A rate that differs from the
previous one is also stored in "updates" with the time it was first seen, so a
morning rate and an evening revision both show with their own time.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, '..', 'grt-rate.json'))
CSV = os.path.normpath(os.path.join(HERE, '..', 'grt-history.csv'))
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')
GRT_URL = 'https://www.grtjewels.com/'
LC_URL = 'https://www.livechennai.com/grt_jewellery_gold_rate.asp'
MONTHS = {m: i for i, m in enumerate(
    ['january', 'february', 'march', 'april', 'may', 'june', 'july',
     'august', 'september', 'october', 'november', 'december'], 1)}
HEARTBEAT_MIN = 50          # rewrite checked_at at most this often when nothing changed
NUM = r'([\d,]+(?:\.\d+)?)'


def fetch(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-IN,en;q=0.9',
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        charset = r.headers.get_content_charset() or 'utf-8'
    return raw.decode(charset, 'replace')


def page_text(src):
    src = re.sub(r'(?is)<(script|style|noscript)\b.*?</\1>', ' ', src)
    src = re.sub(r'(?s)<[^>]+>', ' ', src)
    return re.sub(r'\s+', ' ', html.unescape(src))


def to_num(s):
    v = float(s.replace(',', ''))
    return int(v) if v == int(v) else round(v, 2)


def sane(k22, k24):
    return 3000 < k22 < 60000 and 3000 < k24 < 65000 and 0.88 < k22 / k24 < 0.95


def parse_grt(src):
    """Rate text on GRT's site, e.g. 'GOLD - 22KT - 1 g - ₹ 11,980'."""
    t = page_text(src)
    cur = r'(?:₹|Rs\.?|INR)'

    def find(kt):
        for m in re.finditer(kt + r'\s*K(?:T|ARAT)?\b.{0,40}?' + cur + r'\s*' + NUM, t, re.I):
            yield to_num(m.group(1))

    for k22 in find('22'):
        for k24 in find('24'):
            if sane(k22, k24):
                return {'k22': k22, 'k24': k24}
    return None


def parse_livechennai(src):
    """Rows like '19/November/2026 11980.00 13069.00', newest first."""
    t = page_text(src)
    rows = []
    for d, mon, y, a, b in re.findall(r'(\d{1,2})/([A-Za-z]+)/(\d{4})\s+' + NUM + r'\s+' + NUM, t):
        mi = MONTHS.get(mon.lower())
        if not mi:
            continue
        k22, k24 = to_num(a), to_num(b)
        if sane(k22, k24):
            rows.append({'date': f'{int(y):04d}-{mi:02d}-{int(d):02d}', 'k22': k22, 'k24': k24})
    if not rows:
        return None
    rows.sort(key=lambda r: r['date'], reverse=True)
    out = dict(rows[0])
    older = [r for r in rows if r['date'] < out['date']]
    if older:
        out['prev'] = older[0]
    return out


def read_rate(today, fetcher=fetch):
    errors = []
    try:
        r = parse_grt(fetcher(GRT_URL))
        if r:
            r.update(date=today, source='GRT Jewellers website', source_url=GRT_URL)
            return r, errors
        errors.append('grtjewels.com: no rate found in page')
    except Exception as e:  # network, block, decode
        errors.append(f'grtjewels.com: {e}')
    try:
        r = parse_livechennai(fetcher(LC_URL))
        if r:
            r.update(source='GRT Chennai via livechennai.com', source_url=LC_URL)
            return r, errors
        errors.append('livechennai.com: no rate rows found')
    except Exception as e:
        errors.append(f'livechennai.com: {e}')
    return None, errors


def apply(state, rate, errors, now):
    """Merge a reading into state. Returns True when the file should be written."""
    iso = now.isoformat(timespec='seconds')
    today = now.date().isoformat()
    before = json.dumps(state, sort_keys=True)
    last_check = state.get('checked_at')

    if rate:
        same_day = state.get('date') == rate['date']
        newer_day = not state.get('date') or rate['date'] > state['date']
        reading = {'at': iso, 'k22': rate['k22'], 'k24': rate['k24']}
        if newer_day:
            if state.get('date') and state.get('k22'):
                state['prev'] = {'date': state['date'], 'k22': state['k22'], 'k24': state['k24']}
            elif rate.get('prev'):
                state['prev'] = rate['prev']
            state.update(date=rate['date'], k22=rate['k22'], k24=rate['k24'], updated_at=iso,
                         updates=[dict(reading)], hourly=[])
        elif same_day and (state.get('k22'), state.get('k24')) != (rate['k22'], rate['k24']):
            state.update(k22=rate['k22'], k24=rate['k24'], updated_at=iso)
            state.setdefault('updates', []).append({'at': iso, 'k22': rate['k22'], 'k24': rate['k24']})
        if not state.get('prev') and rate.get('prev'):
            state['prev'] = rate['prev']
        if newer_day or same_day:   # an older reading from a lagging source changes nothing
            state['source'] = rate['source']
            state['source_url'] = rate['source_url']
            if rate['date'] == today:
                hourly = state.get('hourly') if isinstance(state.get('hourly'), list) else []
                state['hourly'] = hourly + [dict(reading)]
        state['status'] = 'ok' if state.get('date') == today else 'stale'
        state['error'] = ''
    else:
        state['status'] = 'error'
        state['error'] = '; '.join(errors)[:300]

    rate_changed = json.dumps(state, sort_keys=True) != before
    old = True
    if last_check:
        try:
            old = (now - dt.datetime.fromisoformat(last_check)).total_seconds() > HEARTBEAT_MIN * 60
        except ValueError:
            pass
    state['checked_at'] = iso
    return rate_changed or old


def main():
    now = dt.datetime.now(IST).replace(microsecond=0)
    try:
        with open(OUT, encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    rate, errors = read_rate(now.date().isoformat())
    for e in errors:
        print('note:', e)
    if rate:
        print(f"read {rate['date']} 22K {rate['k22']} 24K {rate['k24']} from {rate['source']}")
    else:
        print('could not read a rate from any source')
    last_at = ((state.get('hourly') or [{}])[-1] or {}).get('at')
    write = apply(state, rate, errors, now)
    new_at = ((state.get('hourly') or [{}])[-1] or {}).get('at')
    if new_at and new_at != last_at:
        new_file = not os.path.exists(CSV)
        with open(CSV, 'a', encoding='utf-8') as f:
            if new_file:
                f.write('date,time_ist,k22_per_g,k24_per_g,source\n')
            f.write(f"{state['date']},{now.strftime('%H:%M')},{state['k22']},{state['k24']},{state['source']}\n")
        print('grt-history.csv row added')
    if write:
        with open(OUT, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write('\n')
        print('grt-rate.json written')
    else:
        print('no change')
    return 0


if __name__ == '__main__':
    sys.exit(main())
