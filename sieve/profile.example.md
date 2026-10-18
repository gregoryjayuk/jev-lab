# Example interest profile

A made-up reader, here so the sieve runs out of the box. To judge against your own interests, copy this file to `local/profile.md` (git ignores `local/`) and rewrite it as yourself, or point `--profile` or `$JEV_PROFILE` at a file of your own.

The sieve reads only the bullet lines below; everything else in the file, like this paragraph, is for people. The three lines under "Everywhere" describe the reader as a whole and go into every question. Each `- **topic-key** — …` line is the angle on one topic in `sources/sources.json`: the reader's level there, what they want and what they skip. A topic with no line is judged against the general profile only. Change a topic's line and the next run judges that topic's items again. Text in `*(…)*` is a note for people and is not sent.

Whatever is in this file is sent to Jev with every question, so keep it to interests, levels and angles.

## Everywhere
- **Wants:** what is new to them; at or above their level; practical for their actual setup; evidence over hype; the UK version when there is one; depth over listicles.
- **Skips:** beginner basics where they are experienced; "10 tips" listicles and clickbait; product marketing and affiliate roundups; US-only deals; outrage bait; rumours; the same story from five outlets (keep the best one).
- **In one paragraph, for a first cut:** A UK-based backend developer in their forties who runs Linux everywhere and a small home server; plays Go at club level; juggles; shoots film and digital; bakes sourdough at weekends; brews pour-over every morning; reads popular science and history; wants new, at-their-level, practical material and skips beginner basics, listicles and hype.

## Tech
- **linux-foss** — Linux on every machine for fifteen years, comfortable in a terminal, tiling window manager. *Wants:* kernel and desktop changes that affect a daily driver, FOSS replacements for proprietary tools. *Skips:* "which distro should I choose", beginner command-line guides.
- **self-hosting** — A fanless mini-PC running a handful of containers: media server, backups, a password manager. *Wants:* low-power hardware, backup strategy, new self-hosted apps worth trying. *Skips:* enterprise rack gear, "what is Docker".
- **raspberry-pi-makers** — Has a drawer of Raspberry Pis and ESP32 boards. *Wants:* new boards worth buying, e-ink and sensor projects with real code. *Skips:* children's starter kits.
- **programming** — Professional Python and Go developer, ten years in backend services. *Wants:* tools and techniques for working developers, performance, design essays, Python releases. *Skips:* language wars, beginner tutorials, framework hype.
- **ai-llms** — Uses LLM coding assistants daily and runs small models locally. *Wants:* agent engineering, evaluation, cost, local models that fit 16 GB. *Skips:* AGI hype, funding gossip, prompt-tip listicles.
- **3d-printing** — A Bambu Lab printer, a year in. *Wants:* functional prints, slicer tips, materials. *Skips:* resin and industrial printing.

## Mind, hands and practice
- **go** — Plays Go at about 2 kyu, online most weeks, reviews games with KataGo. *Wants:* pro-game commentary, AI-era strategy at single-digit kyu and up, UK and European events. *Skips:* rules of Go, double-digit-kyu lessons.
- **juggling** — Runs five balls, working on four-ball tricks and three clubs. *Wants:* siteswap, five-ball consistency, structured practice, UK conventions. *Skips:* learn-to-juggle-three-balls tutorials.
- **photography** — Shoots manual: a full-frame digital body and a 35 mm film camera. *Wants:* craft, especially street and people; film stocks and labs; workflow for a big archive. *Skips:* rumour churn, exposure basics.

## Body, food and drink
- **baking-bread** — Bakes sourdough most weekends. *Wants:* craft bread technique, flour and fermentation science, enriched doughs. *Skips:* beginner bakes, bread-machine recipes.
- **coffee** — Daily pour-over with a hand grinder. *Wants:* brewing technique and recipes, grinder and water science, UK roasters. *Skips:* chain-café news, espresso machine shopping.

## World
- **international-news** — *Wants:* the big picture: wars, elections, economies, with Asia and Europe given as much room as the US. *Skips:* celebrity, live blogs, sport.
- **science** — *Wants:* clear, deep science and maths writing, space, AI research explained. *Skips:* hype headlines.
- **history** — *Wants:* narrative history, the ancient and medieval worlds, Britain and East Asia; good podcasts. *Skips:* listicles.
