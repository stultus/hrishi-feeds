"""Poll Letterboxd RSS and merge entries into data/movies.json.

Run with --dry-run to print the diff without writing.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from typing import Any

import feedparser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    DATA_DIR,
    MOVIE_FIELDS,
    atomic_write_json,
    canonical_bytes,
    clean_text,
    load_json,
    merge_by_id,
    movie_slug,
    validate,
)

MOVIES_PATH = DATA_DIR / "movies.json"


def feed_url(username: str) -> str:
    return f"https://letterboxd.com/{username}/rss/"


def parse_rating(raw: Any) -> float | None:
    if raw in (None, "", "0"):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v


def parse_year(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def parse_rewatch(raw: Any) -> bool:
    return str(raw).strip().lower() == "yes"


def entry_to_movie(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a feedparser entry to a movie dict, or None if it's not a watched-film entry (e.g. a list)."""
    watched_date = clean_text(entry.get("letterboxd_watcheddate"))
    if not watched_date:
        # Letterboxd RSS includes lists and other items without a watched date; skip them.
        return None

    title = clean_text(entry.get("letterboxd_filmtitle")) or clean_text(entry.get("title"))
    if not title:
        return None

    year = parse_year(entry.get("letterboxd_filmyear"))
    if year is None:
        return None

    letterboxd_url = clean_text(entry.get("link")) or ""
    # The diary-entry URL is unique and stable; use as dedup key.
    entry_id = clean_text(entry.get("id")) or clean_text(entry.get("guid")) or letterboxd_url
    if not entry_id:
        return None

    tmdb_raw = entry.get("tmdb_movieid")
    tmdb_id: str | None = clean_text(str(tmdb_raw)) if tmdb_raw not in (None, "") else None

    review_html = clean_text(entry.get("description")) or clean_text(
        entry.get("summary")
    )

    return {
        "id": entry_id,
        "title": title,
        "year": year,
        "watched_date": watched_date,
        "rating": parse_rating(entry.get("letterboxd_memberrating")),
        "rewatch": parse_rewatch(entry.get("letterboxd_rewatch")),
        "review_html": review_html,
        "tmdb_id": tmdb_id,
        "letterboxd_url": letterboxd_url,
        "slug": movie_slug(title, year),
    }


def sort_movies(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda m: (m["watched_date"], m["id"]), reverse=True)


def fetch(username: str) -> list[dict[str, Any]]:
    parsed = feedparser.parse(feed_url(username))
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"Letterboxd feed parse failed: {parsed.bozo_exception}")
    movies: list[dict[str, Any]] = []
    for entry in parsed.entries:
        movie = entry_to_movie(entry)
        if movie is not None:
            movies.append(movie)
    return movies


def main() -> int:
    ap = argparse.ArgumentParser(description="Poll Letterboxd RSS into movies.json")
    ap.add_argument("--dry-run", action="store_true", help="Print diff instead of writing")
    ap.add_argument(
        "--username",
        default=os.environ.get("LETTERBOXD_USERNAME"),
        help="Letterboxd username (env: LETTERBOXD_USERNAME)",
    )
    args = ap.parse_args()

    if not args.username:
        print("error: LETTERBOXD_USERNAME not set", file=sys.stderr)
        return 2

    existing = load_json(MOVIES_PATH)
    incoming = fetch(args.username)
    merged = sort_movies(merge_by_id(existing, incoming))
    validate(merged, MOVIE_FIELDS)

    new_bytes = canonical_bytes(merged)
    old_bytes = MOVIES_PATH.read_bytes() if MOVIES_PATH.exists() else b""

    if new_bytes == old_bytes:
        print(f"letterboxd: no changes ({len(merged)} movies)")
        return 0

    if args.dry_run:
        diff = difflib.unified_diff(
            old_bytes.decode("utf-8").splitlines(keepends=True),
            new_bytes.decode("utf-8").splitlines(keepends=True),
            fromfile=str(MOVIES_PATH),
            tofile=str(MOVIES_PATH) + " (new)",
        )
        sys.stdout.writelines(diff)
        print(
            f"\nletterboxd: dry-run, would write {len(merged)} movies"
            f" (was {len(existing)})",
            file=sys.stderr,
        )
        return 0

    atomic_write_json(MOVIES_PATH, merged)
    print(f"letterboxd: wrote {len(merged)} movies (was {len(existing)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
