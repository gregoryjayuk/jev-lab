#!/usr/bin/env python3
"""Check every candidate source in candidates/*.txt: find its feed, fetch it, and record whether it is alive and when it last posted.

Writes sources.json (everything, with status), sources.opml (the live ones, for any feed reader) and SOURCES.md (the readable list).
Stdlib only. One request per source in the common case; Reddit is fetched one at a time so it does not rate-limit us.
"""
import concurrent.futures as cf
import email.utils
import gzip
import html
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36 jev-lab-source-check/0.1"
QUIET_AFTER = timedelta(days=183)
FALLBACK_PATHS = ["feed/", "rss/", "feed.xml", "rss.xml", "atom.xml", "index.xml", "feed", "rss"]
reddit_lock = threading.Lock()

SECTIONS = [
    ("Tech", ["linux-foss", "self-hosting", "privacy-degoogle", "raspberry-pi-makers", "home-automation", "programming", "ai-llms", "game-dev", "retro-gaming", "3d-printing", "ebooks-ereaders", "hifi-audio", "stack-releases"]),
    ("Mind, hands and practice", ["magic", "juggling", "memory", "go", "brush-painting", "martial-arts-taichi", "photography", "music-making", "poetry-classics", "philosophy", "psychology", "languages", "writing-books"]),
    ("Body, food and drink", ["fitness-strength", "keto-nutrition", "health-longevity", "ufc-bjj", "chinese-cooking", "baking-bread", "coffee", "cigars"]),
    ("World", ["international-news", "uk-musts", "china", "tibet", "nepal-travel", "history", "science"]),
    ("Life", ["productivity-pkm", "career-leadership", "blogging-web", "finance-fire", "classical-education", "survival", "home-diy", "style-clothing", "baseball-cardinals"]),
    ("Work", ["work-microsoft"]),
]
TOPIC_NAMES = {
    "linux-foss": "Linux and FOSS", "self-hosting": "Self-hosting and home servers", "privacy-degoogle": "Privacy and de-Googling",
    "raspberry-pi-makers": "Raspberry Pi, Jetson and makers", "home-automation": "Home automation", "programming": "Programming",
    "ai-llms": "AI and LLMs", "game-dev": "Game building", "retro-gaming": "Retro gaming and WoW servers", "3d-printing": "3D printing",
    "ebooks-ereaders": "Ebooks and e-readers", "hifi-audio": "CD players, hi-fi and home audio", "stack-releases": "Releases of the software you run",
    "magic": "Magic", "juggling": "Juggling", "memory": "Memory techniques", "go": "Go", "brush-painting": "Chinese brush painting",
    "martial-arts-taichi": "Tai chi, kung fu and martial arts", "photography": "Photography", "music-making": "Drums and guitar",
    "poetry-classics": "Poetry, classics and ideas", "writing-books": "Writing a book", "fitness-strength": "Gym, strength, rehab and flexibility",
    "keto-nutrition": "Keto and nutrition", "health-longevity": "Health and biohacking", "ufc-bjj": "UFC, MMA and jiu-jitsu",
    "chinese-cooking": "Chinese food", "baking-bread": "Bread and baking", "coffee": "Coffee", "cigars": "Cigars",
    "international-news": "International news", "uk-musts": "UK must-knows", "china": "China", "tibet": "Tibet", "nepal-travel": "Nepal, Upper Mustang and budget travel",
    "productivity-pkm": "Productivity, GTD and bullet journal", "blogging-web": "Blogging and website building", "finance-fire": "Finance, FIRE and retirement",
    "classical-education": "Classical education, Latin and Greek", "survival": "Bushcraft and survival", "style-clothing": "Style and clothing",
    "baseball-cardinals": "Baseball and the St. Louis Cardinals", "work-microsoft": "Work: Power Platform, D365, Fabric, Copilot", "philosophy": "Philosophy", "psychology": "Psychology", "history": "History", "languages": "Languages (French, teaching Chinese)", "science": "Science", "career-leadership": "Career and leadership", "home-diy": "Home DIY",
}


def load_candidates():
    seen, out = set(), []
    for f in sorted((HERE / "candidates").glob("*.txt")):
        for line in f.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]  # a blank feed is "| |", so split on the bare bar
            if len(parts) < 5:
                print(f"skipping malformed line in {f.name}: {line[:80]}", file=sys.stderr)
                continue
            topic, name, home, feed, desc = parts[:5]
            key = (feed or home).rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(topic=topic, name=name, homepage=home, feed=feed, description=desc, source_file=f.name))
    return out


def fetch(url, accept="*/*", tries=3):
    """curl, not urllib: the system Python's LibreSSL cannot do TLS 1.3, and urllib in 3.9 does not follow 308 redirects."""
    for attempt in range(tries):
        with tempfile.NamedTemporaryFile() as tmp:
            r = subprocess.run(["curl", "-sS", "-L", "--compressed", "--max-time", "25", "--max-filesize", "40000000",
                                "-A", UA, "-H", f"Accept: {accept}", "-H", "Accept-Language: en-GB,en;q=0.8", "-H", "Cookie: SOCS=CAI",
                                "-o", tmp.name, "-w", "%{http_code} %{url_effective}", url], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"curl exit {r.returncode}: {r.stderr.strip()[:80]}")
            code, _, final = r.stdout.partition(" ")
            if code == "429" and attempt < tries - 1:
                time.sleep(30 * (attempt + 1))
                continue
            if int(code) >= 400:
                raise RuntimeError(f"HTTP {code}")
            return final, Path(tmp.name).read_bytes()
    raise RuntimeError("rate limited")


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        d = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        d = None
    if d is None:
        t = text.replace("Z", "+00:00")
        t = re.sub(r"(\.\d+)(?=[+-]\d\d:?\d\d$|$)", "", t)
        try:
            d = datetime.fromisoformat(t)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def parse_feed(data):
    data = data.lstrip().removeprefix(b"\xef\xbb\xbf").lstrip() if hasattr(data, "removeprefix") else data.lstrip()
    if data[:3] == b"\xef\xbb\xbf":  # a byte-order mark, then the same leading blank line many WordPress feeds carry
        data = data[3:].lstrip()
    root = ET.fromstring(data)
    if local(root.tag) not in ("rss", "feed", "rdf"):
        raise ValueError(f"not a feed (root <{local(root.tag)}>)")
    items = [el for el in root.iter() if local(el.tag) in ("item", "entry")]
    dates = []
    for it in items:
        for child in it:
            if local(child.tag) in ("pubdate", "published", "updated", "date", "issued"):
                d = parse_date(child.text)
                if d:
                    dates.append(d)
                    break
    title = next((el.text for el in root.iter() if local(el.tag) == "title" and el.text), "")
    return len(items), (max(dates) if dates else None), (title or "").strip()


LINK_RE = re.compile(r"<link\b[^>]*>", re.I)


def discover(page_url, body):
    text = body.decode("utf-8", "replace")
    found = []
    for tag in LINK_RE.findall(text):
        attrs = dict((k.lower(), html.unescape(v)) for k, _, v in re.findall(r'(\w[\w-]*)\s*=\s*(["\'])(.*?)\2', tag))
        if "alternate" in attrs.get("rel", "").lower() and re.search(r"(rss|atom)\+xml", attrs.get("type", ""), re.I) and attrs.get("href"):
            href = urllib.parse.urljoin(page_url, attrs["href"])
            if "comments" not in href.lower():
                found.append(href)
    m = re.search(r'"externalId"\s*:\s*"(UC[\w-]{22})"', text)  # the channel's own id; "channelId" also names other channels on the page
    if m and "youtube.com" in page_url:
        found.append(f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}")
    return found


def try_feed(url):
    if "reddit.com" in url:
        with reddit_lock:
            final, data = fetch(url, "application/rss+xml, application/xml")
            time.sleep(8)  # Reddit rate-limits anonymous feed readers hard
    else:
        final, data = fetch(url, "application/rss+xml, application/atom+xml, application/xml, text/xml")
    n, latest, title = parse_feed(data)
    return final, n, latest, title


def check(c):
    tried, errors = [], []
    candidates = [c["feed"]] if c["feed"] else []
    homepage_fetched = False
    while True:
        for url in candidates:
            if url in tried:
                continue
            tried.append(url)
            try:
                final, n, latest, title = try_feed(url)
                return finish(c, final, n, latest, title, None)
            except Exception as e:
                errors.append(f"{url}: {type(e).__name__}: {str(e)[:80]}")
        if homepage_fetched:
            break
        homepage_fetched = True
        try:
            page_url, body = fetch(c["homepage"], "text/html,application/xhtml+xml")
            candidates = discover(page_url, body)
            try:  # the homepage itself may be the feed
                n, latest, title = parse_feed(body)
                return finish(c, page_url, n, latest, title, None)
            except Exception:
                pass
            if not candidates and "youtube.com" not in c["homepage"] and "reddit.com" not in c["homepage"]:
                base = page_url if page_url.endswith("/") else page_url + "/"
                candidates = [urllib.parse.urljoin(base, p) for p in FALLBACK_PATHS]
        except Exception as e:
            errors.append(f"homepage {c['homepage']}: {type(e).__name__}: {str(e)[:80]}")
            break
    return finish(c, None, 0, None, "", errors[-1] if errors else "no feed found")


def finish(c, feed, n, latest, title, error):
    now = datetime.now(timezone.utc)
    if error:
        status = "failed"
    elif n == 0:
        status = "empty"  # a valid feed with nothing in it right now (normal for a warnings feed)
    elif latest is None:
        status = "undated"
    elif now - latest > QUIET_AFTER:
        status = "quiet"
    else:
        status = "live"
    return {**c, "feed_found": feed, "items": n, "latest": latest.date().isoformat() if latest else None, "feed_title": title, "status": status, "error": error}


def render(results):
    by_topic = {}
    for r in results:
        by_topic.setdefault(r["topic"], []).append(r)
    usable = [r for r in results if r["status"] in ("live", "quiet", "undated", "empty")]
    live = sum(r["status"] == "live" for r in results)
    lines = [
        "# Sources",
        "",
        f"Checked {datetime.now():%Y-%m-%d %H:%M}: **{len(usable)} working feeds** out of {len(results)} candidates ({live} posted in the last six months). Regenerate with `python3 check_sources.py`; this file is written by it, so edit `candidates/*.txt`, not this.",
        "",
        "Latest = the newest item in the feed when checked. A quiet source has not posted for six months; it stays listed, and the sieve simply sees nothing from it.",
        "",
    ]
    known = set()
    for section, topics in SECTIONS:
        lines += [f"## {section}", ""]
        for t in topics:
            known.add(t)
            rows = [r for r in by_topic.get(t, []) if r["status"] in ("live", "quiet", "undated", "empty")]
            if not rows:
                continue
            lines += [f"### {TOPIC_NAMES.get(t, t)} ({len(rows)})", "", "| Source | What it is | Latest |", "|---|---|---|"]
            for r in sorted(rows, key=lambda r: r["name"].lower()):
                when = r["latest"] or ("nothing right now" if r["status"] == "empty" else "undated")
                if r["status"] == "quiet":
                    when += " (quiet)"
                lines.append(f"| [{r['name']}]({r['homepage']}) | {r['description']} | [{when}]({r['feed_found']}) |")
            lines.append("")
    extra = sorted(set(by_topic) - known)
    for t in extra:
        rows = [r for r in by_topic[t] if r["status"] in ("live", "quiet", "undated", "empty")]
        if rows:
            lines += [f"### {TOPIC_NAMES.get(t, t)} ({len(rows)})", "", "| Source | What it is | Latest |", "|---|---|---|"]
            lines += [f"| [{r['name']}]({r['homepage']}) | {r['description']} | [{r['latest'] or 'undated'}]({r['feed_found']}) |" for r in rows]
            lines.append("")
    failed = [r for r in results if r["status"] == "failed"]
    if failed:
        lines += ["## Couldn't verify", "", "No working feed found when checked; they may still be fine sites, but the sieve cannot read them yet.", "", "| Source | Topic | Why |", "|---|---|---|"]
        for r in sorted(failed, key=lambda r: (r["topic"], r["name"].lower())):
            why = (r["error"] or "").replace("|", "/")[:110]
            lines.append(f"| [{r['name']}]({r['homepage']}) | {TOPIC_NAMES.get(r['topic'], r['topic'])} | {why} |")
        lines.append("")
    (HERE / "SOURCES.md").write_text("\n".join(lines))

    opml = ['<?xml version="1.0" encoding="UTF-8"?>', '<opml version="2.0">', f"<head><title>Greg's sources ({datetime.now():%Y-%m-%d})</title></head>", "<body>"]
    for t in sorted(by_topic):
        rows = [r for r in by_topic[t] if r["status"] in ("live", "quiet", "undated", "empty")]
        if not rows:
            continue
        opml.append(f'<outline text="{html.escape(TOPIC_NAMES.get(t, t))}" title="{html.escape(TOPIC_NAMES.get(t, t))}">')
        for r in rows:
            opml.append(f'  <outline type="rss" text="{html.escape(r["name"])}" title="{html.escape(r["name"])}" xmlUrl="{html.escape(r["feed_found"])}" htmlUrl="{html.escape(r["homepage"])}"/>')
        opml.append("</outline>")
    opml += ["</body>", "</opml>"]
    (HERE / "sources.opml").write_text("\n".join(opml))


def main():
    cands = load_candidates()
    incremental = "--incremental" in sys.argv  # check only new candidates and last run's failures
    previous = {}
    if incremental and (HERE / "sources.json").exists():
        previous = {(r["topic"], r["name"]): r for r in json.loads((HERE / "sources.json").read_text())}
    todo = [c for c in cands if not incremental or (c["topic"], c["name"]) not in previous or previous[(c["topic"], c["name"])]["status"] == "failed"]
    print(f"Checking {len(todo)} of {len(cands)} candidates…", flush=True)
    results = []
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(check, c): c for c in todo}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            results.append(fut.result())
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}", flush=True)
    if incremental:
        done = {(r["topic"], r["name"]) for r in results}
        results += [r for k, r in previous.items() if k not in done and any(k == (c["topic"], c["name"]) for c in cands)]
    results.sort(key=lambda r: (r["topic"], r["name"].lower()))
    (HERE / "sources.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    render(results)
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("Done:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
