#!/usr/bin/env python3
"""Give the Korean and Anime section caches the same lists as Hollywood and Bollywood.

Those two sections store each list as its own subsection (movies: popular, trending, latest;
series: tv-popular, tv-trending, tv-latest). Korean and Anime only had one `movies` and one `series`
list. This script derives the missing lists from the rows already stored, offline and without TMDB:

    highest-rated   Korean movies and Anime series only: the existing curated list, unchanged
                    (subsection `highest-rated` / `tv-highest-rated`)
    popular         those two: the curated list re-ordered by IMDb votes (audience size);
                    Korean series and Anime movies: the existing curated order, unchanged
    trending        recent popularity: IMDb votes divided by (1 + age in years), so newer titles with a big
                    audience rise above old classics (falls back to the TMDB rating when votes are unknown)
    latest          released titles only, newest release date first

The result is only a stand-in until the TMDB-driven refresh (scripts/refresh_seed.py) writes real
lists into the same subsections. A refresh overwrites these; running this script again never overwrites
a list that came from a refresh (the `derivedFrom` marker is absent) and re-derives from it instead.

    python scripts/derive_regional_modes.py [--seed backend/cache-seed] [--today YYYY-MM-DD] [--check]
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED = ROOT / "backend" / "cache-seed"

# section -> one spec per media type. `sources` are tried in order; the first one that exists and is not
# itself derived is the curated list. `highest_rated` names the subsection that keeps that curated list
# (None = this list is not shown as Highest Rated).
SPECS = {
    "korean": (
        {"media": "movie", "prefix": "", "sources": ("highest-rated", "movies"), "highest_rated": "highest-rated"},
        {"media": "tv", "prefix": "tv-", "sources": ("tv-popular", "series"), "highest_rated": None},
    ),
    "anime": (
        {"media": "movie", "prefix": "", "sources": ("popular", "movies"), "highest_rated": None},
        {"media": "tv", "prefix": "tv-", "sources": ("tv-highest-rated", "series"), "highest_rated": "tv-highest-rated"},
    ),
}
MODES = ("popular", "trending", "latest")
MARKER = "derivedFrom"


def parse_votes(value) -> int:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def trending_score(row: dict, today: date) -> float:
    released = parse_date(row.get("releaseDate"))
    age_years = max(0.0, (today - released).days / 365.25) if released else 50.0
    votes = parse_votes(row.get("imdbVotes"))
    weight = votes if votes > 0 else float(row.get("rating") or 0) * 1000
    return weight / (1.0 + age_years)


def ordered(rows: list[dict], mode: str, today: date, popular_by_votes: bool = False) -> list[dict]:
    if mode == "popular":
        if popular_by_votes:
            return sorted(rows, key=lambda r: parse_votes(r.get("imdbVotes")), reverse=True)
        return list(rows)
    if mode == "latest":
        released = [r for r in rows if (parse_date(r.get("releaseDate")) or date.max) <= today]
        # sorted() is stable, so equal dates keep the curated order
        return sorted(released, key=lambda r: parse_date(r.get("releaseDate")) or date.min, reverse=True)
    return sorted(rows, key=lambda r: trending_score(r, today), reverse=True)


def build_subsection(source: dict, rows: list[dict], key: str, source_name: str) -> dict:
    page_size = int(source.get("pageSize") or 18)
    total = len(rows)
    page_count = max(1, -(-total // page_size))
    subsection = {k: copy.deepcopy(v) for k, v in source.items() if k != "results"}
    subsection.update({
        "catalogueKey": key,
        "pageSize": page_size,
        "pageCount": page_count,
        "totalResults": total,
        MARKER: source_name,
        "results": copy.deepcopy(rows),
    })
    if "pageOffsets" in source:
        subsection["pageOffsets"] = [min(i * page_size, total) for i in range(page_count)] + [total]
    return subsection


def pick_source(subsections: dict, names: tuple[str, ...]) -> str | None:
    for name in names:
        entry = subsections.get(name)
        if isinstance(entry, dict) and entry.get("results") and MARKER not in entry:
            return name
    return None


def derive_document(document: dict, section: str, today: date) -> list[str]:
    """Add the derived subsections in place; return what was written."""
    written: list[str] = []
    subsections = document.setdefault("subsections", {})
    for spec in SPECS[section]:
        prefix, media_type, highest_rated = spec["prefix"], spec["media"], spec["highest_rated"]
        source_name = pick_source(subsections, spec["sources"])
        if source_name is None:
            continue
        source = subsections[source_name]
        rows = source["results"]
        if highest_rated and source_name != highest_rated:
            subsections[highest_rated] = build_subsection(source, rows, highest_rated, source_name)
            subsections[highest_rated]["mediaType"] = media_type
            written.append(f"{section}.{highest_rated} ({len(rows)} rows)")
        for mode in MODES:
            name = f"{prefix}{mode}"
            if name == source_name:
                continue  # real data from the refresh pipeline: leave it alone
            existing = subsections.get(name)
            if isinstance(existing, dict) and MARKER not in existing and existing.get("results"):
                continue
            ordered_rows = ordered(rows, mode, today, popular_by_votes=bool(highest_rated))
            subsections[name] = build_subsection(source, ordered_rows, name, source_name)
            subsections[name]["mediaType"] = media_type
            written.append(f"{section}.{name} ({len(ordered_rows)} rows)")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--check", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args(argv)

    changed = 0
    for section in SPECS:
        path = args.seed / f"{section}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        written = derive_document(document, section, args.today)
        for line in written:
            print(("would write " if args.check else "wrote ") + line)
        if written and not args.check:
            # Same compact layout the other section caches are stored in, so diffs stay small.
            path.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        changed += len(written)
    print(f"{changed} subsection(s) {'to write' if args.check else 'written'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
