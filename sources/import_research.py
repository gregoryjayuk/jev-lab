#!/usr/bin/env python3
"""Turn a research batch (tab-separated: group, name, homepage, feed, description, evidence) into a candidates/*.txt file.

Usage: python3 import_research.py <batch.tsv> <out-name> <group:topic>...
e.g.   python3 import_research.py a.tsv research_a 1:brush-painting 2:magic
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    src, out_name, *pairs = sys.argv[1:]
    topics = dict(p.split(":", 1) for p in pairs)
    lines = [f"# Imported from a research batch ({Path(src).name}); every line is checked by check_sources.py before it counts.", ""]
    skipped = 0
    for raw in Path(src).read_text().splitlines():
        f = [x.strip() for x in raw.split("\t")]
        if len(f) < 5 or f[0] not in topics or not f[2].startswith("http"):
            skipped += raw.strip() != ""
            continue
        desc = f[4].replace("|", "/")
        lines.append(f"{topics[f[0]]} | {f[1].replace('|', '/')} | {f[2]} | {f[3] if f[3].startswith('http') else ''} | {desc}")
    (HERE / "candidates" / f"{out_name}.txt").write_text("\n".join(lines) + "\n")
    print(f"{len(lines) - 2} candidates written to candidates/{out_name}.txt ({skipped} lines skipped)")


if __name__ == "__main__":
    main()
