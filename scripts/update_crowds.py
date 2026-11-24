#!/usr/bin/env python3
"""Fetch WDW crowd predictions and save them to crowds.json.

Runs on GitHub Actions once a day. Python standard library only.

The source calendar (MagicGuides) is built by the Calendarize.it WordPress plugin,
which loads events with JavaScript. This script tries, in order:
  1. Calendarize.it's own event feed (rhc_action=get_calendar_events / get_icalendar_events)
  2. Feed-like addresses found on the page, plus a few common guesses
Event titles look like "Magic Kingdom » [3]" (park + crowd level 1-5).

If fewer than MIN_DAYS days are found, the script exits with an error and leaves the
existing crowds.json untouched, so the planner keeps showing the last good data.
The log prints diagnostics about everything it tried, which is what we use to adjust it.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request

SITE = "https://magicguides.com/"
PAGE = SITE + "wdw-crowd-calendar/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "crowds.json")
UA = "Mozilla/5.0 (compatible; DecadesOfMagicPlanner/1.0)"
MIN_DAYS = 14
MAX_SPAN_DAYS = 31

PARKS = {
    "magic kingdom": "mk",
    "epcot": "ep",
    "hollywood studios": "hs",
    "animal kingdom": "ak",
}
TITLE_RE = re.compile(
    r"(magic kingdom|epcot|hollywood studios|animal kingdom)\D{0,12}(\d)", re.I
)
ENTRY_RE = re.compile(r"(?:»|&raquo;|&#187;|&#xbb;)\s*\[\d\]", re.I)

CANDIDATES = [
    PAGE + "?ical=1",
    "https://magicguides.com/wp-json/wp/v2/events?per_page=100",
]
FEED_HINTS = ("ical", ".ics", "admin-ajax", "rhc_action", "fullcalendar")
SKIP_EXT = (".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2", ".ico", ".webp", ".js")
RHC_ACTIONS = ("get_calendar_events", "get_icalendar_events")


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def short(text, n=160):
    return re.sub(r"\s+", " ", text[:n])


def snippets(text, pattern, width=160, limit=4):
    out = []
    for m in re.finditer(pattern, text):
        a, b = max(0, m.start() - width), min(len(text), m.end() + width)
        out.append(re.sub(r"\s+", " ", text[a:b]))
        if len(out) >= limit:
            break
    return out


def d8(s):
    m = re.match(r"\s*(\d{4})-?(\d{2})-?(\d{2})", s or "")
    if not m:
        return None
    try:
        return dt.date(*map(int, m.groups()))
    except ValueError:
        return None


def to_date_str(v):
    """Accept ISO strings or unix timestamps (seconds or milliseconds)."""
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
        n = int(float(v))
        if n > 10**11:
            n //= 1000
        try:
            return dt.datetime.fromtimestamp(n, dt.timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    return str(v)


def span(start_s, end_s):
    """Dates covered by an event. End dates are treated as exclusive."""
    start = d8(start_s)
    if not start:
        return []
    end = d8(end_s)
    if not end or end <= start:
        return [start]
    n = min((end - start).days, MAX_SPAN_DAYS)
    return [start + dt.timedelta(days=i) for i in range(n)]


def add(days, summary, start_s, end_s):
    m = TITLE_RE.search(html.unescape(summary or ""))
    if not m:
        return
    level = int(m.group(2))
    if not 1 <= level <= 5:
        return
    key = PARKS[m.group(1).lower()]
    for d in span(start_s, end_s):
        days.setdefault(d.isoformat(), {})[key] = level


def parse_ics(text, days):
    text = re.sub(r"\r?\n[ \t]", "", text)
    for blk in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        def prop(name):
            m = re.search(r"^" + name + r"[^:\r\n]*:(.*)$", blk, re.M)
            return m.group(1).strip() if m else None
        add(days, prop("SUMMARY"), prop("DTSTART"), prop("DTEND"))


def walk(o):
    if isinstance(o, dict):
        t = o.get("title") or o.get("name") or o.get("summary")
        if isinstance(t, dict):
            t = t.get("rendered")
        s = o.get("start") or o.get("start_date") or o.get("startDate")
        e = o.get("end") or o.get("end_date") or o.get("endDate") or ""
        if isinstance(t, str) and s:
            yield t, to_date_str(s), to_date_str(e) if e else ""
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def parse_json(text, days):
    for t, s, e in walk(json.loads(text)):
        add(days, t, s, e)


def discover(page_html):
    found = set()
    for u in re.findall(r"https?:\\?/\\?/[^\"'\s<>)]+", page_html):
        u = u.replace("\\/", "/").rstrip("\\,;")
        low = u.lower()
        if any(h in low for h in FEED_HINTS) and not low.split("?")[0].endswith(SKIP_EXT):
            found.add(u)
    return sorted(found)


def try_url(url, days, show=False):
    before = len(days)
    try:
        body = fetch(url)
    except Exception as exc:  # network errors are expected for guessed URLs
        print(f"  x {url} -> {exc}")
        return
    try:
        if "BEGIN:VCALENDAR" in body:
            parse_ics(body, days)
        else:
            parse_json(body, days)
    except Exception as exc:
        print(f"  ? {url} -> fetched {len(body)} chars, could not parse ({exc})")
        if show or len(body) < 3000:
            print(f"      starts with: {short(body)}")
        return
    added = len(days) - before
    print(f"  ok {url} -> +{added} new days")
    if added == 0 and (show or len(body) < 3000):
        print(f"      starts with: {short(body)}")


def try_rhc(days):
    """Calendarize.it's own event feed, asked for a wide date range."""
    start = dt.date.today() - dt.timedelta(days=7)
    end = start + dt.timedelta(days=500)

    def ts(d):
        return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())

    print("Calendarize.it feed attempts:")
    for action in RHC_ACTIONS:
        for s, e in ((start.isoformat(), end.isoformat()), (ts(start), ts(end))):
            url = f"{SITE}?rhc_action={action}&post_type=events&start={s}&end={e}"
            try_url(url, days, show=True)
            if len(days) >= MIN_DAYS:
                return


def diagnostics(page_html):
    print("--- Diagnostics ---")
    for label, pat in (
        ("rhc/ajax settings on the page", r"rhc_action|ajaxurl|get_calendar_events"),
        ("calendar entries on the page", ENTRY_RE.pattern),
    ):
        hits = snippets(page_html, pat, width=220, limit=2)
        print(f"{label}: {len(hits)} sample(s)")
        for h in hits:
            print("   ", h)
    js = [
        u.replace("\\/", "/")
        for u in re.findall(r"https?:\\?/\\?/[^\"'\s<>)]+calendarize-it[^\"'\s<>)]*frontend[^\"'\s<>)]*\.js[^\"'\s<>)]*", page_html)
    ]
    if js:
        try:
            body = fetch(js[0])
            hits = snippets(body, r"rhc_action", width=200, limit=5)
            print(f"plugin script mentions of rhc_action: {len(hits)}")
            for h in hits:
                print("   ", h)
        except Exception as exc:
            print("could not read the plugin script:", exc)
    try:
        body = fetch(SITE + "wp-json/wp/v2/events?per_page=3")
        items = json.loads(body)
        if isinstance(items, list) and items:
            first = items[0]
            print("events REST endpoint: first item keys:", sorted(first.keys()))
            print("   first item title:", (first.get("title") or {}).get("rendered"))
            for k in ("meta", "acf"):
                if first.get(k):
                    print(f"   first item {k}:", short(json.dumps(first[k]), 300))
        else:
            print("events REST endpoint returned:", short(body, 200))
    except Exception as exc:
        print("events REST endpoint:", exc)
    print("--- End diagnostics ---")


def main():
    days = {}
    print(f"Reading {PAGE}")
    page, urls = "", []
    try:
        page = fetch(PAGE)
        urls = discover(page)
    except Exception as exc:
        print(f"Could not read the calendar page: {exc}")
    print(f"Feed-like URLs found on the page: {len(urls)}")
    for u in urls:
        print("  -", u)

    try_rhc(days)

    if len(days) < MIN_DAYS:
        tried = set()
        print("Other addresses:")
        for url in urls[:25] + CANDIDATES:
            if url in tried:
                continue
            tried.add(url)
            try_url(url, days)

    today = dt.date.today()
    cutoff = (today - dt.timedelta(days=7)).isoformat()
    days = {k: v for k, v in sorted(days.items()) if k >= cutoff}
    print(f"Total days with predictions: {len(days)}")

    if len(days) < MIN_DAYS:
        diagnostics(page)
        print("Not enough data found; leaving crowds.json unchanged.")
        sys.exit(2)

    old = {}
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f).get("days", {})
    except Exception:
        pass
    if old == days:
        print("No changes since the last run.")
        return

    payload = {
        "source": "MagicGuides",
        "sourceUrl": PAGE,
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": days,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"Wrote {len(days)} days to crowds.json")


if __name__ == "__main__":
    main()
