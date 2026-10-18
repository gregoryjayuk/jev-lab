#!/usr/bin/env python3
"""Ask Jev which category each of Greg's bookmarks belongs in, hiding the category it is already in, and report how often it agrees and how honest its confidence is.

Reads a SQLite database with a `bookmarks` table (id, name, url, category) read-only and never writes to it: --db, else
$BOOKMARKS_DB, else local/bookmarks.db (git ignores local/). Stdlib only.
The API key comes from $TYPESAFE_API_KEY, else the macOS Keychain item `typesafe-api-key`; it is never printed or saved.
"""
import argparse
import concurrent.futures as cf
import json
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "local" / "bookmarks.db"
API = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
PRICE_PER_MTOK = 0.042  # USD per million input tokens; output tokens are free
# Work: employer URLs never leave this machine. "1" and Temp: grab-bags with nothing to predict.
EXCLUDE = {"Work", "1", "Temp"}
INSTRUCTIONS = "Which category in Greg's bookmarks does `bookmark` belong in?"


def load_bookmarks(db):
    if not Path(db).exists():
        sys.exit(f"No bookmarks database at {db}: pass --db or set $BOOKMARKS_DB")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT id, name, url, category FROM bookmarks ORDER BY id").fetchall()
    con.close()
    return [dict(id=r[0], title=r[1], url=r[2], category=r[3]) for r in rows if r[3] not in EXCLUDE]


def load_criteria(path, categories):
    if not path:
        return {f: None for f in categories}  # round 1: category names only, no descriptions
    criteria = json.loads(Path(path).read_text())
    missing = set(categories) - set(criteria)
    if missing:
        sys.exit(f"{path} has no entry for: {', '.join(sorted(missing))}")
    return {f: criteria[f] for f in categories}


def api_key():
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    r = subprocess.run(["security", "find-generic-password", "-s", "typesafe-api-key", "-w"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit('No API key found. Store it once with:  security add-generic-password -a "$USER" -s typesafe-api-key -w')
    return r.stdout.strip()


def request_body(bm, criteria):
    return {
        "model": MODEL,
        "state": {"bookmark": {"title": bm["title"], "url": bm["url"]}},
        "questions": {
            "category": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": criteria},
            # A second question about the same state costs nothing extra: Jev reads the state once and answers both.
            "title_matches": {"type": "noul", "instructions": "Does `bookmark.title` describe the page at `bookmark.url`?"},
        },
    }


def call(body, key, tries=5):
    data = json.dumps(body).encode()
    for attempt in range(tries):
        req = urllib.request.Request(API, data=data, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp), (time.perf_counter() - t0) * 1000
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:  # rate limited or server trouble: wait and retry
                time.sleep(min(float(e.headers.get("retry-after") or 2 ** attempt), 30))
                continue
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
        except urllib.error.URLError:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"gave up after {tries} tries")


def evaluate(bm, criteria, key):
    out, ms = call(request_body(bm, criteria), key)
    ans = out["answers"]["category"]
    probs = ans["probabilities"]
    top3 = sorted(probs.items(), key=lambda kv: -kv[1])[:3]
    return {**bm, "jev": ans["choice"], "confidence": ans["confidence"], "p_true_category": probs.get(bm["category"], 0.0),
            "title_matches": out["answers"]["title_matches"]["noul"],
            "top3": top3, "ms": round(ms), "input_tokens": out["usage"]["input_tokens"], "model": out["model"]}


def pct(n, d):
    return f"{100 * n / d:5.1f}%" if d else "    –"


def report(results, failures):
    n = len(results)
    right = [r for r in results if r["jev"] == r["category"]]
    print(f"\nModel {results[0]['model']} · {n} bookmarks · {len(failures)} failed calls")
    print(f"Agrees with your category: {len(right)}/{n} = {pct(len(right), n).strip()}")

    print("\nIs the confidence honest?  (each band: how often Jev was right when it was that sure)")
    print("  confidence    bookmarks   right")
    for lo, hi in [(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)]:
        band = [r for r in results if lo <= r["confidence"] < hi]
        print(f"  {lo:.1f}–{min(hi, 1):.1f}     {len(band):9d}   {pct(sum(r['jev'] == r['category'] for r in band), len(band))}")

    print("\nPick a threshold: file automatically above it, leave the rest in Unsorted for you")
    print("  threshold   filed automatically   right among those")
    for t in (.3, .5, .7, .8, .9):
        auto = [r for r in results if r["confidence"] >= t]
        print(f"  {t:.1f}         {len(auto):4d} ({pct(len(auto), n)})        {pct(sum(r['jev'] == r['category'] for r in auto), len(auto))}")

    print("\nBy category  (categories defined by how you use them, not what they are, should score low)")
    by = Counter(r["category"] for r in results)
    ok = Counter(r["category"] for r in right)
    for category, cnt in by.most_common():
        print(f"  {category[:22]:22s} {ok[category]:3d}/{cnt:<3d} {pct(ok[category], cnt)}")

    confusions = Counter((r["category"], r["jev"]) for r in results if r["jev"] != r["category"])
    if confusions:
        print("\nMost common mix-ups  (your category → Jev's pick)")
        for (a, b), c in confusions.most_common(12):
            print(f"  {c:3d}  {a} → {b}")

    sure_wrong = sorted((r for r in results if r["jev"] != r["category"]), key=lambda r: -r["confidence"])[:12]
    if sure_wrong:
        print("\nWhere Jev disagrees most confidently  (often the bookmark is the one that's misfiled)")
        for r in sure_wrong:
            print(f"  {r['confidence']:.2f}  {r['title'][:45]:45s}  in {r['category']!r} · Jev says {r['jev']!r}")

    mismatched = sorted(results, key=lambda r: r["title_matches"])[:8]
    print("\nTitles that may not match their link  (the second question, answered in the same call)")
    for r in mismatched:
        print(f"  {r['title_matches']:.2f}  {r['title'][:40]:40s}  {r['url'][:60]}")

    ms = sorted(r["ms"] for r in results)
    tokens = sum(r["input_tokens"] for r in results)
    print(f"\nSpeed: median {statistics.median(ms):.0f} ms, slowest 5% over {ms[int(.95 * (len(ms) - 1))]} ms per call (network included)")
    print(f"Cost: {tokens:,} input tokens = ${tokens * PRICE_PER_MTOK / 1e6:.5f}")
    for f in failures[:5]:
        print(f"  failed: {f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--criteria", help="JSON file mapping every category to a description (round 2); default is category names only")
    ap.add_argument("--limit", type=int, help="a random sample of N bookmarks (a cheap smoke test)")
    ap.add_argument("--workers", type=int, default=8, help="calls in flight at once (rate limit is 80 a second)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent; no API call, no key needed")
    ap.add_argument("--db", default=os.environ.get("BOOKMARKS_DB") or str(DB), help="the SQLite bookmarks database (default: $BOOKMARKS_DB, else local/bookmarks.db)")
    a = ap.parse_args()

    bms = load_bookmarks(Path(a.db).expanduser())
    categories = sorted({b["category"] for b in bms})
    criteria = load_criteria(a.criteria, categories)
    random.Random(0).shuffle(bms)
    if a.limit:
        bms = bms[: a.limit]

    if a.dry_run:
        print(f"{len(bms)} bookmarks across {len(categories)} categories (left out: {', '.join(sorted(EXCLUDE))})")
        print("Categories:", ", ".join(f"{f} ({c})" for f, c in Counter(b['category'] for b in bms).most_common()))
        print("\nOne request exactly as it would be sent:")
        print(json.dumps(request_body(bms[0], criteria), indent=2, ensure_ascii=False))
        return

    key = api_key()
    results, failures = [], []
    print(f"Asking Jev about {len(bms)} bookmarks, {a.workers} at a time…")
    with cf.ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(evaluate, bm, criteria, key): bm for bm in bms}
        for fut in cf.as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                failures.append(f"{futures[fut]['title'][:40]}: {e}")
    if not results:
        sys.exit("Every call failed:\n" + "\n".join(failures[:5]))

    report(results, failures)
    out = HERE / "results" / f"{datetime.now():%Y%m%d-%H%M%S}-{Path(a.criteria).stem if a.criteria else 'names-only'}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"criteria": criteria, "results": results, "failures": failures}, indent=1, ensure_ascii=False))
    print(f"\nFull results: {out}")


if __name__ == "__main__":
    main()
