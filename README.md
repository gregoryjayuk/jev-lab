# jev-lab

Two small experiments with Jev, TypeSafe AI's hosted decision model: a model that answers typed questions (pick one option, place on a scale, yes/no) with calibrated probabilities instead of prose. The main one is a feed sieve: it reads a week of posts from over 500 public RSS, Atom and YouTube feeds, has Jev judge every item against a reader's interest profile, reads the best ones in full, and writes a short weekly reading list. The second asks Jev to re-file a bookmarks collection with the answers hidden and measures how often it agrees, whether its confidence is honest, and what it costs. Python standard library only.

![The weekly reading list the sieve writes](docs/screenshots/reading-list.png)

![Everything Jev judged this week, with its scores](docs/screenshots/judged-this-week.png)

Both screenshots are from a real run with the made-up example profile (`sieve/profile.example.md`) over 14 topics: 8,345 feed items read, 1,063 from the last seven days judged in 1,096 calls, about a minute, $0.04.

## The sieve (`sieve/`)

- Reads every working feed in `sources/sources.json` (Reddit one feed every eight seconds, or it rate-limits) and keeps the last seven days.
- Asks Jev four questions about each new item in one call: how worth the reader's time it is, given their angle on that topic (a Score from "not for them" to "glad to know"), and three yes/no checks: aimed at beginners? mainly marketing or a listicle? a deadline or rule change someone could act on?
- Reads the full article text of the best forty and asks again.
- Picks the list: a score floor, the best item of each practice topic (hobbies are judged against the week's best in that hobby, not against the news), at most three per topic and two per source, thirty in all, same-story duplicates from different outlets collapsed, pre-releases dropped. Dated items go to the top.
- Remembers every judgement (`sieve/state/judged.json`), so a daily run only pays for what is new; the list is rebuilt from the whole week each run, so a good item stays on it for seven days. Changing a topic's line in the profile re-judges that topic.
- Writes `sieve/out/reading-list.html` and `.md`, `shortlist.json`, and files that explain a run: `judged-this-week.html` and `.csv` (every item with all four answers), `feeds-this-run.csv` (per-feed counts and errors).

```
python3 sieve/sieve.py --dry-run --skip-reddit        # fetch and count, show one request; no key needed
python3 sieve/sieve.py --fake --skip-reddit           # random scores, to check the page without a key or a bill
python3 sieve/sieve.py --topics go,juggling           # a small real run
python3 sieve/sieve.py                                # everything (Reddit makes it about 12 minutes)
python3 sieve/sieve.py --render-only                  # rebuild the list from what is already judged
python3 sieve/sieve.py --example "some title"         # save exactly what one request sent and got back
```

### The interest profile

The sieve judges against a markdown profile: three lines about the reader as a whole (Wants, Skips, a one-paragraph sketch) and one line per topic giving their level and what they want and skip there. A topic list alone is not enough: an article on three-ball juggling basics is on topic and useless to someone who runs five. The profile is `--profile`, else `$JEV_PROFILE`, else `local/profile.md` (git ignores `local/`), else the made-up example in [`sieve/profile.example.md`](sieve/profile.example.md), so it runs out of the box. The whole profile is sent to Jev with every question, so keep it to interests, levels and angles.

## The sources (`sources/`)

`sources/candidates/*.txt` holds several hundred candidate sources across about fifty topics, one per line: `topic | name | homepage | feed | what it is`, the feed left blank to be discovered. `check_sources.py` finds each feed (link tags, YouTube channel ids, common paths), fetches it and records whether it is alive and when it last posted, then writes [`SOURCES.md`](sources/SOURCES.md) (readable), `sources.opml` (import into any feed reader) and `sources.json` (for the sieve); `--incremental` re-checks only new lines and last run's failures. `import_research.py` turns a tab-separated research batch into a candidates file. Every feed is public; some sites paywall part of what they publish.

## The bookmark test (`bookmark_eval.py`)

Asks Jev which category each bookmark belongs in, with the category it is already in hidden, plus whether its title matches its link (both questions in one call). Reports agreement, a confidence-versus-accuracy table, what an auto-file threshold would cover, the mix-ups, the bookmarks Jev is most sure are misfiled, speed and cost. Read-only against the database; categories named Work, "1" and Temp are never sent.

```
python3 bookmark_eval.py --dry-run --db my-bookmarks.db       # what would be sent; no key needed
python3 bookmark_eval.py --limit 20                           # a smoke test, a fraction of a cent
python3 bookmark_eval.py --criteria categories.json           # round 2: categories with descriptions
```

The database is `--db`, else `$BOOKMARKS_DB`, else `local/bookmarks.db`: any SQLite file with a `bookmarks` table of `id, name, url, category`. Full results go to `results/`, which git ignores.

First run, 18 Oct 2026, on 211 of the author's bookmarks in 32 categories, category names only:

| | |
|---|---|
| Agrees with the existing category | 105 / 211 (49.8%) |
| Existing category in Jev's top three | 141 / 211 (66.8%) |
| Failed calls | 0 |
| Speed | median 305 ms a call, 95th percentile 501 ms (network included) |
| Cost | 117,890 input tokens (about 560 a call) = $0.005 |

| Jev's confidence | Bookmarks | Right |
|---|---|---|
| 0.2–0.4 | 19 | 21.1% |
| 0.4–0.6 | 51 | 9.8% |
| 0.6–0.8 | 27 | 29.6% |
| 0.8–1.0 | 114 | 77.2% |

| File automatically at confidence ≥ | Filed | Right among those |
|---|---|---|
| 0.5 | 168 (79.6%) | 58.9% |
| 0.7 | 123 (58.3%) | 74.0% |
| 0.9 | 83 (39.3%) | 81.9% |

Subject categories (a language, a hobby) scored 90–100%; categories defined by how they are used rather than what is in them (a daily-reads folder, a picks folder) scored near zero, as they should with names alone. Confidence is roughly honest at the top and not below it. Round 2, with a sentence describing each category, is the next test.

## Setup

- Python 3.9 or later and `curl`; nothing to install.
- A TypeSafe API key, read from `$TYPESAFE_API_KEY`, else (on macOS) the Keychain item `typesafe-api-key`. Store it once with `security add-generic-password -a "$USER" -s typesafe-api-key -w`, which prompts, so the key never lands in shell history. The sieve hands it to curl through a private temporary config file, never on a command line, and neither script prints or saves it.
- Jev's price at the time of writing: $0.042 per million input tokens, output free. A full sieve run over every topic judged about 3,000 new items for $0.11.

## Tech

Python standard library (`urllib`, `xml.etree`, `sqlite3`, `concurrent.futures`), curl for fetching (TLS 1.3 and redirects that the system Python cannot do), and Jev's HTTP API (`POST https://api.typesafe.ai/v1/systemone`) with Score, Noul and Choice questions.

## Licence

MIT, see [LICENSE](LICENSE).
