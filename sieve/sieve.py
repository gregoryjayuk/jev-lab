#!/usr/bin/env python3
"""The sieve: read every working feed in ../sources/sources.json, keep the last week's items, ask Jev how worth the reader's time
each one is against their angle on that topic (the interest profile, see --profile), and write a reading list.

Run it daily. Each item is judged once and remembered in state/judged.json, so a run only pays for what is new; the list is
rebuilt every run from everything judged in the last seven days, so a good item stays on it for a week.

    python3 sieve.py --dry-run                 # fetch and count, no Jev calls, no key needed
    python3 sieve.py --topics juggling,go      # a small real run
    python3 sieve.py                           # the whole thing

The profile is --profile, else $JEV_PROFILE, else ../local/profile.md (git ignores local/), else the made-up example in
profile.example.md, so the sieve runs out of the box.

The key comes from $TYPESAFE_API_KEY or the Keychain item `typesafe-api-key`; it goes to curl through a private temp
file, never on a command line, and is never printed.
"""
import argparse
import concurrent.futures as cf
import hashlib
import html
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "sources"))
from check_sources import SECTIONS, TOPIC_NAMES, fetch, local, parse_date  # noqa: E402

SOURCES = HERE.parent / "sources" / "sources.json"
PROFILE = HERE.parent / "local" / "profile.md"  # your own profile; git ignores local/
EXAMPLE_PROFILE = HERE / "profile.example.md"
STATE = HERE / "state" / "judged.json"
OUT = HERE / "out"
API = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
PRICE_PER_MTOK = 0.042
WINDOW = timedelta(days=7)
KEEP_STATE = timedelta(days=21)
MIN_WORTH, MAX_PROMO, MAX_BEGINNER = 1.7, 0.6, 0.7
# His practice is judged against the week's best in that practice, not against the news: a good juggling video rarely
# outscores a policy story, and a list of only news is not his life. Each practice topic gets its best item above this floor.
PRACTICE_FLOOR = 1.0
PRACTICE = {"magic", "juggling", "memory", "go", "brush-painting", "martial-arts-taichi", "photography", "music-making", "languages",
            "writing-books", "fitness-strength", "keto-nutrition", "chinese-cooking", "baking-bread", "coffee", "cigars", "hifi-audio",
            "survival", "3d-printing", "retro-gaming"}  # things he does, not things he reads about
PER_TOPIC, PER_SOURCE, TOTAL, TOP = 3, 2, 30, 5
QVERSION = "2"  # bump when the questions change, so everything is judged again
PRERELEASE = re.compile(r"(?i)(\brc\b|-rc\.?\d|\bbeta\b|\balpha\b|nightly|pre-?release)")
DEEP_N = 40  # the best candidates get their full article read and judged again
NO_ARTICLE = ("youtube.com", "reddit.com", "github.com", "libsyn.com", "anchor.fm", "megaphone.fm", "buzzsprout.com", "podbean.com")

WORTH = {
    "type": "score",
    "instructions": "How worth Greg's time is `item`, given `angle` (his level in this topic and what he wants and skips there) and `reader`?",
    "criteria": [
        "Not for him: beneath his level, off his angle, marketing, or about nothing he follows",
        "Ordinary: fine, but nothing new for him",
        "Good: at his level and new to him",
        "Glad to know: he would be glad he saw this this week",
    ],
}
QUESTIONS = {
    "worth": WORTH,
    "beginner": {"type": "noul", "instructions": "Is `item` aimed at beginners in its subject?"},
    "promo": {"type": "noul", "instructions": "Is `item` mainly marketing, a deal, a listicle or clickbait?"},
    "dated": {"type": "noul", "instructions": "Does `item` announce a deadline, an event, or a change in rules or law that someone with `angle` could act on (not a product or software release)?"},
}


# ---------- the reader's angles ----------

def clean(text):
    text = re.sub(r"\*\(.*?\)\*", "", text)  # dated "(Read … until …)" notes are for humans, not for Jev
    text = re.sub(r"\s*\([^()]*\)\s*$", "", text.strip())  # the trailing (sources) reference
    return re.sub(r"\s+", " ", text.replace("*", "")).strip()


def profile_path(arg=None):
    if arg or os.environ.get("JEV_PROFILE"):
        return Path(arg or os.environ["JEV_PROFILE"]).expanduser()
    return PROFILE if PROFILE.exists() else EXAMPLE_PROFILE


def load_angles(path):
    reader, topics = [], {}
    for line in Path(path).read_text().splitlines():
        m = re.match(r"- \*\*([a-z0-9-]+)\*\* — (.*)", line)
        if m:
            topics[m.group(1)] = clean(m.group(2))
            continue
        m = re.match(r"- \*\*(Wants|Skips|In one paragraph, for a first cut):\*\* (.*)", line)
        if m:
            reader.append(f"{m.group(1)}: {clean(m.group(2))}")
    return " ".join(reader), topics


# ---------- feeds ----------

def strip_html(text, limit):
    text = html.unescape(html.unescape(text or ""))
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"submitted by\s+/u/\S+|\[link\]|\[comments\]", " ", text)
    text = re.sub(r"https?://\S+", " ", text)  # affiliate and tracking links are noise to Jev
    return re.sub(r"\s+", " ", text).strip()[:limit]


def child_text(el, *names):
    for c in el.iter():
        if c is not el and local(c.tag) in names and (c.text or "").strip():
            return c.text.strip()
    return ""


def parse_items(data, feed_url):
    data = data.lstrip()
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:].lstrip()
    root = ET.fromstring(data)
    out = []
    for el in root.iter():
        kind = local(el.tag)
        if kind not in ("item", "entry"):
            continue
        links = [c for c in el if local(c.tag) == "link"]
        link = next((c.get("href") for c in links if c.get("href") and c.get("rel", "alternate") == "alternate"), "") \
            or next(((c.text or "").strip() for c in links if (c.text or "").strip()), "") \
            or next((c.get("href") for c in links if c.get("href")), "")
        title = strip_html(child_text(el, "title"), 300)
        summary = strip_html(child_text(el, "description", "summary", "encoded", "content"), 700)
        published = None
        for name in ("pubdate", "published", "updated", "date", "issued"):
            published = parse_date(child_text(el, name))
            if published:
                break
        guid = child_text(el, "guid", "id")
        if title and (link or guid):
            out.append({"title": title, "link": urllib.parse.urljoin(feed_url, link) if link else guid, "guid": guid, "summary": summary, "published": published.isoformat() if published else None})
    return out


def item_key(link):
    u = urllib.parse.urlsplit(link)
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(u.query) if not k.lower().startswith(("utm_", "ref", "fbclid"))])
    return urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path.rstrip("/"), query, ""))


def gather(sources, skip_reddit):
    reddit = [s for s in sources if "reddit.com" in s["feed_found"]]
    others = [s for s in sources if "reddit.com" not in s["feed_found"]]
    items, failures, lock, counts = [], [], threading.Lock(), {}

    def one(src):
        try:
            _, data = fetch(src["feed_found"], "application/rss+xml, application/atom+xml, application/xml, text/xml")
            got = parse_items(data, src["feed_found"])
            with lock:
                counts[src["name"]] = (src, len(got), "")
                for it in got:
                    items.append({**it, "source": src["name"], "topic": src["topic"], "homepage": src["homepage"]})
        except Exception as e:
            with lock:
                counts[src["name"]] = (src, 0, str(e)[:60])
                failures.append(f"{src['name']}: {str(e)[:60]}")

    def reddit_run():
        for src in reddit:
            one(src)
            time.sleep(8)  # Reddit rate-limits anonymous feed readers hard

    threads = [] if skip_reddit else [threading.Thread(target=reddit_run)]
    for t in threads:
        t.start()
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(one, others))
    for t in threads:
        t.join()
    return items, failures, counts


# ---------- Jev ----------

class Jev:
    def __init__(self, key):
        fd, self.cfg = tempfile.mkstemp(prefix="jev-", suffix=".curlrc")  # created 0600
        with os.fdopen(fd, "w") as f:
            f.write(f'header = "Authorization: Bearer {key}"\n')
        self.tokens, self.calls, self.lock = 0, 0, threading.Lock()

    def close(self):
        Path(self.cfg).unlink(missing_ok=True)

    def ask(self, state, questions):
        body = json.dumps({"model": MODEL, "state": state, "questions": questions})
        for attempt in range(5):
            r = subprocess.run(["curl", "-sS", "--max-time", "40", "-K", self.cfg, "-H", "Content-Type: application/json",
                                "--data-binary", "@-", "-w", "\n%{http_code}", API], input=body, capture_output=True, text=True)
            out, _, code = r.stdout.rpartition("\n")
            if r.returncode != 0 or code == "429" or code.startswith("5"):
                time.sleep(min(2 ** attempt * 2, 30))
                continue
            if code != "200":
                raise RuntimeError(f"Jev said HTTP {code}: {out[:200]}")
            res = json.loads(out)
            with self.lock:
                self.tokens += res.get("usage", {}).get("input_tokens", 0)
                self.calls += 1
            return res["answers"]
        raise RuntimeError("Jev did not answer after 5 tries")


class FakeJev:
    """Plausible random answers, for checking the pipeline and the page without a key or a bill."""
    tokens = calls = 0

    def ask(self, state, questions):
        rnd = random.Random(json.dumps(state, sort_keys=True))
        out = {}
        for qid, q in questions.items():
            if q["type"] == "score":
                out[qid] = {"type": "score", "score": round(rnd.triangular(0, 3, 1.4), 2), "confidence": round(rnd.random(), 2)}
            else:
                out[qid] = {"type": "noul", "noul": round(rnd.random() ** 3, 2)}
        return out

    def close(self):
        pass


def judge(item, reader, angles, jev, text=None):
    angle = angles.get(item["topic"], "")
    state = {"reader": reader, "angle": {"topic": TOPIC_NAMES.get(item["topic"], item["topic"]), "his_angle_on_it": angle},
             "item": {"title": item["title"], "source": item["source"], "summary": item["summary"]}}
    if text:
        state["item"]["article_text"] = text
        ans = jev.ask(state, {"worth": WORTH})
        return {"worth": ans["worth"]["score"], "worth_conf": ans["worth"].get("confidence"), "deep": True}
    ans = jev.ask(state, QUESTIONS)
    return {"worth": ans["worth"]["score"], "worth_conf": ans["worth"].get("confidence"), "beginner": ans["beginner"]["noul"],
            "promo": ans["promo"]["noul"], "dated": ans["dated"]["noul"], "deep": False}


def article_text(url):
    _, body = fetch(url, "text/html,application/xhtml+xml")
    page = body.decode("utf-8", "replace")
    page = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form|noscript)\b.*?</\1>", " ", page)
    paras = [strip_html(p, 2000) for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", page)]
    text = " ".join(p for p in paras if len(p) > 60)
    return text[:6000]


# ---------- the list ----------

def angle_hash(angles, topic):
    return hashlib.sha1((QVERSION + angles.get(topic, "")).encode()).hexdigest()[:10]


def when(it):
    return datetime.fromisoformat(it["published"] or it["first_seen"])


def select(pool):
    def ok(x):
        if x.get("worth") is None or x.get("promo", 0) >= MAX_PROMO or x.get("beginner", 0) >= MAX_BEGINNER:
            return False
        if x["topic"] == "stack-releases" and PRERELEASE.search(x["title"]):
            return False
        return x["worth"] >= (PRACTICE_FLOOR if x["topic"] in PRACTICE else MIN_WORTH)

    ranked = sorted((x for x in pool if ok(x)), key=lambda x: (-x["worth"], -when(x).timestamp()))
    # the same story from two outlets shares a rare word (a name, a place) within a few days: keep the better one
    words = lambda x: {w for w in re.findall(r"[a-z][a-z0-9'’-]{4,}", x["title"].lower())}
    df = {}
    for x in pool:
        for w in words(x):
            df[w] = df.get(w, 0) + 1
    chosen, per_topic, per_source = [], {}, {}

    def take(x):
        if per_topic.get(x["topic"], 0) >= PER_TOPIC or per_source.get(x["source"], 0) >= PER_SOURCE or x in chosen:
            return False
        rare = {w for w in words(x) if df.get(w, 0) <= 3}
        for y in chosen:
            if rare & words(y) and abs((when(x) - when(y)).days) <= 4:
                return False
        chosen.append(x)
        per_topic[x["topic"]] = per_topic.get(x["topic"], 0) + 1
        per_source[x["source"]] = per_source.get(x["source"], 0) + 1
        return True

    for t in PRACTICE:  # first, the best of the week in each practice
        best = next((x for x in ranked if x["topic"] == t), None)
        if best:
            take(best)
    for x in ranked:  # then everything else, best first, news held to the higher bar
        if len(chosen) >= TOTAL:
            break
        if x["topic"] in PRACTICE and per_topic.get(x["topic"], 0) >= 1 and x["worth"] < MIN_WORTH:
            continue
        take(x)
    chosen.sort(key=lambda x: (-x["worth"], -when(x).timestamp()))
    must = [x for x in chosen if x.get("dated", 0) >= 0.75 and x["worth"] >= 2.0][:5]
    return chosen, must


def stars(w):
    return "★★★" if w >= 2.5 else "★★" if w >= 2.0 else "★"


def render(chosen, must, stats, fake):
    now = datetime.now(timezone.utc)
    start = (now - WINDOW).astimezone()
    title = f"Reading list, {start:%-d %b} to {now.astimezone():%-d %b %Y}"
    rest = [x for x in chosen if x not in must]  # a dated item is shown once, at the top
    top = rest[:TOP]
    by_topic = {}
    for x in rest[TOP:]:
        by_topic.setdefault(x["topic"], []).append(x)

    def card(x):
        d = when(x).astimezone()
        summary = html.escape(x["summary"][:240] + ("…" if len(x["summary"]) > 240 else ""))
        return (f'<article><h3><a href="{html.escape(x["link"])}">{html.escape(x["title"])}</a></h3>'
                f'<p class="meta"><span class="stars" title="Jev: {x["worth"]:.2f} of 3">{stars(x["worth"])}</span> '
                f'{html.escape(x["source"])} · {html.escape(TOPIC_NAMES.get(x["topic"], x["topic"]))} · {d:%a %-d %b}</p>'
                + (f"<p>{summary}</p>" if summary else "") + "</article>")

    parts = []
    if must:
        parts.append('<section class="must"><h2>Deadlines, events and rule changes</h2>' + "".join(card(x) for x in must) + "</section>")
    parts.append("<section><h2>The best five this week</h2>" + "".join(card(x) for x in top) + "</section>")
    for section, topics in SECTIONS:
        cards = [card(x) for t in topics for x in by_topic.get(t, [])]
        if cards:
            parts.append(f"<section><h2>{html.escape(section)}</h2>{''.join(cards)}</section>")
    foot = (f"Read {stats['items']:,} items from {stats['sources']} sources ({stats['window']:,} from the last seven days); "
            f"Jev judged {stats['new']:,} new ones this run in {stats['calls']:,} calls for ${stats['cost']:.4f}. "
            f"{stats['failed']} feeds could not be read. Generated {now.astimezone():%a %-d %b %H:%M}.")
    banner = '<p class="fake">FAKE SCORES: a test of the page, not Jev\'s judgement.</p>' if fake else ""
    page = f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reading list</title><style>
:root{{--bg:#fbfaf7;--fg:#22201c;--mute:#6b665d;--line:#e4e0d8;--accent:#9a3412;--must:#fff4e5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1b1a18;--fg:#ece8e1;--mute:#a39d92;--line:#36332e;--accent:#f0a36b;--must:#2b231b}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:17px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:46rem;margin:0 auto;padding:2rem 1rem 4rem}} h1{{font-size:1.6rem;margin:0 0 .25rem}} .sub{{color:var(--mute);margin:0 0 2rem}}
h2{{font-size:1.05rem;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);border-bottom:1px solid var(--line);padding-bottom:.35rem;margin:2.2rem 0 .5rem}}
article{{padding:.7rem 0;border-bottom:1px solid var(--line)}} article:last-child{{border-bottom:0}}
h3{{font-size:1.05rem;margin:0 0 .15rem;font-weight:600}} a{{color:var(--fg);text-decoration:none}} a:hover{{color:var(--accent);text-decoration:underline}}
.meta{{color:var(--mute);font-size:.85rem;margin:0 0 .25rem}} .stars{{color:var(--accent);letter-spacing:.05em}} article p{{margin:.1rem 0}}
.must{{background:var(--must);border-radius:10px;padding:.2rem 1rem .4rem}} .must h2{{border:0}} footer{{color:var(--mute);font-size:.8rem;margin-top:3rem}}
.fake{{background:#fde68a;color:#422006;padding:.5rem .8rem;border-radius:6px}}
</style></head><body><main><h1>{html.escape(title)}</h1><p class="sub">{len(chosen)} things worth your time, chosen by Jev from everything your sources published this week.</p>
{banner}{''.join(parts)}<footer>{html.escape(foot)}</footer></main></body></html>"""
    OUT.mkdir(exist_ok=True)
    (OUT / "reading-list.html").write_text(page)
    md = [f"# {title}", "", f"{len(chosen)} things worth your time, chosen by Jev.", ""] + (["FAKE SCORES: a test of the page, not Jev's judgement.", ""] if fake else [])
    if must:
        md += ["## Deadlines, events and rule changes", ""] + [f"- [{x['title']}]({x['link']}) — {x['source']}" for x in must] + [""]
    md += ["## The best five this week", ""] + [f"- [{x['title']}]({x['link']}) — {x['source']}" for x in top] + [""]
    for section, topics in SECTIONS:
        rows = [x for t in topics for x in by_topic.get(t, [])]
        if rows:
            md += [f"## {section}", ""] + [f"- [{x['title']}]({x['link']}) — {x['source']}, {TOPIC_NAMES.get(x['topic'], x['topic'])}" for x in rows] + [""]
    md += [foot, ""]
    (OUT / "reading-list.md").write_text("\n".join(md))
    (OUT / "shortlist.json").write_text(json.dumps({"generated": now.isoformat(), "fake": fake, "must": must, "chosen": chosen}, indent=1, ensure_ascii=False))


# ---------- files that explain a run ----------

def write_feed_counts(counts, fresh):
    week = {}
    for it in fresh.values():
        week[it["source"]] = week.get(it["source"], 0) + 1
    rows = sorted(counts.values(), key=lambda r: (-week.get(r[0]["name"], 0), -r[1]))
    lines = ["topic,source,items in the feed,items from the last seven days,error"]
    for src, n, err in rows:
        name = src["name"].replace('"', "'")
        lines.append(f'{src["topic"]},"{name}",{n},{week.get(src["name"], 0)},"{err}"')
    OUT.mkdir(exist_ok=True)
    (OUT / "feeds-this-run.csv").write_text("\n".join(lines) + "\n")


def write_judged(week, chosen):
    on_list = {id(x) for x in chosen}
    rows = sorted(week, key=lambda x: (x["topic"], -(x.get("worth") or 0)))
    lines = ["topic,worth (0-3),beginner,promo,dated,read in full,on the list,published,source,title,link"]
    for x in rows:
        q = lambda v: '"' + str(v).replace('"', "'") + '"'
        lines.append(",".join([x["topic"], f'{x.get("worth", 0):.2f}', f'{x.get("beginner", 0):.2f}', f'{x.get("promo", 0):.2f}', f'{x.get("dated", 0):.2f}',
                               "yes" if x.get("deep") and x.get("worth_conf") is not None else "", "yes" if id(x) in on_list else "",
                               when(x).date().isoformat(), q(x["source"]), q(x["title"]), q(x["link"])]))
    (OUT / "judged-this-week.csv").write_text("\n".join(lines) + "\n")
    blocks = []
    for section, topics in SECTIONS:
        for t in topics:
            xs = [x for x in rows if x["topic"] == t]
            if not xs:
                continue
            trs = "".join(
                f'<tr class="{"on" if id(x) in on_list else ""}"><td class="n">{x.get("worth", 0):.2f}</td><td class="n">{x.get("beginner", 0):.2f}</td>'
                f'<td class="n">{x.get("promo", 0):.2f}</td><td class="n">{x.get("dated", 0):.2f}</td>'
                f'<td><a href="{html.escape(x["link"])}">{html.escape(x["title"])}</a><br><span class="meta">{html.escape(x["source"])} · {when(x):%a %-d %b}</span></td></tr>'
                for x in xs)
            blocks.append(f'<details><summary>{html.escape(TOPIC_NAMES.get(t, t))} <span class="meta">{len(xs)} judged, {sum(id(x) in on_list for x in xs)} on the list</span></summary>'
                          f'<table><tr><th>worth</th><th>beginner</th><th>promo</th><th>dated</th><th>item</th></tr>{trs}</table></details>')
    (OUT / "judged-this-week.html").write_text(f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Everything Jev judged</title><style>
:root{{--bg:#fbfaf7;--fg:#22201c;--mute:#6b665d;--line:#e4e0d8;--on:#fff4e5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1b1a18;--fg:#ece8e1;--mute:#a39d92;--line:#36332e;--on:#2b231b}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}} main{{max-width:60rem;margin:0 auto;padding:1.5rem 1rem 3rem}}
summary{{cursor:pointer;padding:.45rem 0;border-bottom:1px solid var(--line);font-weight:600}} table{{border-collapse:collapse;width:100%;margin:.4rem 0 1rem}}
th{{text-align:left;color:var(--mute);font-weight:500;font-size:.8rem}} td{{border-top:1px solid var(--line);padding:.3rem .4rem;vertical-align:top}} .n{{font-variant-numeric:tabular-nums;width:4.2rem}}
tr.on{{background:var(--on)}} a{{color:var(--fg)}} .meta{{color:var(--mute);font-size:.8rem;font-weight:400}}
</style></head><body><main><h1>Everything Jev judged this week</h1>
<p class="meta">{len(rows):,} items from the last seven days, by topic, best first. <b>worth</b> is Jev's Score from 0 (not for him) to 3 (glad to know); <b>beginner</b>, <b>promo</b> and <b>dated</b> are its Noul answers, 0 to 1. Shaded rows made the reading list. Open a topic you know well and see whether you agree.</p>
{''.join(blocks)}</main></body></html>""")


def example(text, reader, angles, jev):
    judged = json.loads(STATE.read_text())
    x = next((x for x in judged.values() if text.lower() in x["title"].lower()), None)
    if not x:
        sys.exit(f"No judged item has {text!r} in its title")
    angle = angles.get(x["topic"], "")
    body = {"model": MODEL, "state": {"reader": reader, "angle": {"topic": TOPIC_NAMES.get(x["topic"], x["topic"]), "his_angle_on_it": angle},
            "item": {"title": x["title"], "source": x["source"], "summary": x["summary"]}}, "questions": QUESTIONS}
    answers = jev.ask(body["state"], body["questions"])
    raw = {"what was sent (POST https://api.typesafe.ai/v1/systemone, with the key in an Authorization header)": body, "what came back (answers)": answers}
    OUT.mkdir(exist_ok=True)
    (OUT / "example.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False))
    print(f"Saved {OUT / 'example.json'}")


# ---------- the run ----------

def api_key():
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    r = subprocess.run(["security", "find-generic-password", "-s", "typesafe-api-key", "-w"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit('No API key found. Store it once with:  security add-generic-password -a "$USER" -s typesafe-api-key -w')
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="fetch and count; no Jev calls")
    ap.add_argument("--fake", action="store_true", help="random scores instead of Jev, to test the page (state is not saved)")
    ap.add_argument("--topics", help="comma-separated topic keys to limit the run")
    ap.add_argument("--skip-reddit", action="store_true", help="leave Reddit out (it must be read slowly: ~8 s a subreddit)")
    ap.add_argument("--max-new", type=int, default=8000, help="a ceiling on new items judged in one run")
    ap.add_argument("--no-deep", action="store_true", help="skip reading the best candidates' full articles")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--render-only", action="store_true", help="rebuild the list from what is already judged: no fetching, no Jev calls")
    ap.add_argument("--example", metavar="TITLE", help="send one judged item to Jev again and save exactly what went and came back to out/example.json")
    ap.add_argument("--profile", help="the interest profile to judge against (default: $JEV_PROFILE, else local/profile.md, else profile.example.md)")
    a = ap.parse_args()
    profile = profile_path(a.profile)

    if a.example:
        reader, angles = load_angles(profile)
        jev = Jev(api_key())
        try:
            example(a.example, reader, angles, jev)
        finally:
            jev.close()
        return
    if a.render_only:
        judged = json.loads(STATE.read_text())
        now = datetime.now(timezone.utc)
        week = [x for x in judged.values() if now - when(x) <= WINDOW]
        chosen, must = select(week)
        render(chosen, must, {"items": 0, "sources": 0, "window": len(week), "new": 0, "calls": 0, "cost": 0.0, "failed": 0}, False)
        write_judged(week, chosen)
        print(f"Rebuilt from {len(week):,} judged items: {len(chosen)} on the list, {len(must)} with dates.")
        return
    sources = [s for s in json.loads(SOURCES.read_text()) if s["status"] in ("live", "quiet", "undated", "empty") and s.get("feed_found")]
    if a.topics:
        wanted = set(a.topics.split(","))
        sources = [s for s in sources if s["topic"] in wanted]
    reader, angles = load_angles(profile)
    if profile == EXAMPLE_PROFILE:
        print(f"Judging against the made-up example profile ({EXAMPLE_PROFILE.name}); pass --profile or set $JEV_PROFILE for your own")
    missing = sorted({s["topic"] for s in sources} - set(angles))
    if missing:
        print(f"No angle yet for: {', '.join(missing)} (they are judged against the general profile only)")

    t0 = time.time()
    print(f"Reading {len(sources)} feeds…", flush=True)
    items, failures, counts = gather(sources, a.skip_reddit)
    now = datetime.now(timezone.utc)
    judged = json.loads(STATE.read_text()) if STATE.exists() else {}
    fresh = {}
    for it in items:
        k = item_key(it["link"])
        if k in fresh:
            continue
        first_seen = judged.get(k, {}).get("first_seen") or now.isoformat()
        it["first_seen"] = first_seen
        if now - datetime.fromisoformat(it["published"] or first_seen) <= WINDOW:
            fresh[k] = it
    write_feed_counts(counts, fresh)
    todo = [(k, it) for k, it in fresh.items() if k not in judged or judged[k].get("angle") != angle_hash(angles, it["topic"])]
    print(f"{len(items):,} items, {len(fresh):,} from the last seven days, {len(todo):,} not yet judged ({time.time() - t0:.0f} s)", flush=True)
    if len(todo) > a.max_new:
        print(f"Judging only the newest {a.max_new:,} (--max-new)")
        todo = sorted(todo, key=lambda kv: kv[1]["published"] or kv[1]["first_seen"], reverse=True)[: a.max_new]

    if a.dry_run:
        if todo:
            k, it = todo[0]
            print("\nOne question exactly as it would be sent:\n" + json.dumps({"model": MODEL, "state": {"reader": reader, "angle": {"topic": TOPIC_NAMES.get(it["topic"], it["topic"]), "his_angle_on_it": angles.get(it["topic"], "")}, "item": {"title": it["title"], "source": it["source"], "summary": it["summary"]}}, "questions": QUESTIONS}, indent=1, ensure_ascii=False)[:3000])
        per_topic = {}
        for it in fresh.values():
            per_topic[it["topic"]] = per_topic.get(it["topic"], 0) + 1
        print("\nLast seven days, by topic:", ", ".join(f"{t} {n}" for t, n in sorted(per_topic.items(), key=lambda kv: -kv[1])))
        print(f"Feeds that failed: {len(failures)}")
        return

    jev = FakeJev() if a.fake else Jev(api_key())
    try:
        def do(kv):
            k, it = kv
            try:
                return k, {**it, **judge(it, reader, angles, jev), "angle": angle_hash(angles, it["topic"])}
            except Exception as e:
                return k, {"error": str(e)[:120]}

        errors = 0
        with cf.ThreadPoolExecutor(max_workers=a.workers) as pool:
            for i, (k, res) in enumerate(pool.map(do, todo), 1):
                if "error" in res:
                    errors += 1
                    if errors == 1:
                        print(f"  first error: {res['error']}", flush=True)
                else:
                    judged[k] = res
                if i % 250 == 0:
                    print(f"  judged {i:,}/{len(todo):,}", flush=True)
        if errors:
            print(f"{errors} items could not be judged this run; they will be tried again next run")

        pool_items = [x for k, x in judged.items() if k in fresh]
        if not a.no_deep:
            deep = [x for x in sorted(pool_items, key=lambda x: -(x.get("worth") or 0)) if not x.get("deep") and not any(d in x["link"] for d in NO_ARTICLE)][:DEEP_N]
            deep = [x for x in deep if x.get("worth", 0) >= MIN_WORTH - 0.2]
            if deep:
                print(f"Reading the full article for the best {len(deep)}…", flush=True)

                def go(x):
                    try:
                        text = article_text(x["link"])
                        if len(text) >= 500:
                            x.update(judge(x, reader, angles, jev, text=text))
                    except Exception:
                        pass
                    x["deep"] = True

                with cf.ThreadPoolExecutor(max_workers=a.workers) as pool:
                    list(pool.map(go, deep))
    finally:
        jev.close()

    if not a.fake:
        keep = {k: x for k, x in judged.items() if now - when(x) <= KEEP_STATE}
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(keep, ensure_ascii=False))
    chosen, must = select([x for k, x in judged.items() if k in fresh])
    stats = {"items": len(items), "sources": len(counts), "window": len(fresh), "new": len(todo), "calls": jev.calls,
             "cost": jev.tokens * PRICE_PER_MTOK / 1e6, "failed": len(failures)}
    render(chosen, must, stats, a.fake)
    write_judged([x for k, x in judged.items() if k in fresh], chosen)
    print(f"Done in {time.time() - t0:.0f} s: {len(chosen)} on the list, {len(must)} with dates. {jev.calls:,} Jev calls, {jev.tokens:,} tokens, ${stats['cost']:.4f}.")
    print(f"Open {OUT / 'reading-list.html'}")


if __name__ == "__main__":
    main()
