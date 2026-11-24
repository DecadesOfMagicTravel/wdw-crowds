#!/usr/bin/env python3
"""Fetch WDW crowd predictions and save them to crowds.json.

Runs on GitHub Actions once a day. Python standard library only.

The source calendar (MagicGuides) draws itself with JavaScript, so this script
looks for the data feed behind it: it reads the page, lists feed-like URLs it
finds, tries them (plus a few common guesses), and understands two formats:
iCal (.ics) and FullCalendar-style JSON. Event titles look like
"Magic Kingdom » [3]" (park + crowd level 1-5).

If fewer than MIN_DAYS days are found, the script exits with an error and leaves
the existing crowds.json untouched, so the planner keeps showing the last good
data. The log always lists what it found, which tells us what to adjust.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request

PAGE = "https://magicguides.com/wdw-crowd-calendar/"
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

CANDIDATES = [
    PAGE + "?ical=1",
    "https://magicguides.com/?ical=1",
    "https://magicguides.com/events/?ical=1",
    "https://magicguides.com/wp-json/wp/v2/events?per_page=100",
]
FEED_HINTS = ("ical", ".ics", "admin-ajax", "calendarize", "rhc", "wp-json", "fullcalendar")
SKIP_EXT = (".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2", ".ico", ".webp")


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def d8(s):
    m = re.match(r"\s*(\d{4})-?(\d{2})-?(\d{2})", s or "")
    if not m:
        return None
    try:
        return dt.date(*map(int, m.groups()))
    except ValueError:
        return None


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
            yield t, str(s), str(e)
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


def try_url(url, days):
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
        return
    print(f"  ok {url} -> +{len(days) - before} new days")


def main():
    days = {}
    print(f"Reading {PAGE}")
    try:
        page = fetch(PAGE)
        urls = discover(page)
    except Exception as exc:
        print(f"Could not read the calendar page: {exc}")
        page, urls = "", []
    print(f"Feed-like URLs found on the page: {len(urls)}")
    for u in urls:
        print("  -", u)

    tried = set()
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
