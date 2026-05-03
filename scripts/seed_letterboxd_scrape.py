"""One-time backfill: scrape the public Letterboxd diary into data/movies.json.

Usage:
    LETTERBOXD_USERNAME=stultus uv run scripts/seed_letterboxd_scrape.py
    # or
    uv run scripts/seed_letterboxd_scrape.py --username stultus

Each diary row carries data-viewing-id="NNNNN", which the RSS feed surfaces as
'letterboxd-watch-NNNNN'. Using `letterboxd-watch-{viewing_id}` as our id keeps
backfill and ongoing RSS polls in the same id space, so subsequent polls
upsert cleanly without producing duplicates.

Fields we can extract from the diary listing alone:
    id, title, year, watched_date, rating, rewatch, slug, letterboxd_url
review_html and tmdb_id need the per-entry page; we leave them null here. The
nightly RSS poll fills in those fields for the ~50 most recent watches; older
entries keep them null until you backfill via individual entry pages (not done
here — would require ~1 request per film with a review).

Pagination: probes /films/diary/page/N/ until a page returns zero rows. With a
1.5s delay between requests, ~10 pages takes ~15 seconds. Pass --delay 0 to
disable; do not do that unless you enjoy 429s.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    DATA_DIR,
    MOVIE_FIELDS,
    atomic_write_json,
    canonical_bytes,
    clean_text,
    movie_slug,
    validate,
)

MOVIES_PATH = DATA_DIR / "movies.json"

# A real-browser UA. Letterboxd 403s on default requests/python UAs.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)

NAME_YEAR_RE = re.compile(r"^(?P<title>.+?)\s+\((?P<year>\d{4})\)\s*$")
DATE_HREF_RE = re.compile(r"/diary/films/for/(\d{4})/(\d{2})/(\d{2})/")
RATED_CLASS_RE = re.compile(r"rated-(\d+)")


def diary_url(username: str, page: int) -> str:
    return f"https://letterboxd.com/{username}/films/diary/page/{page}/"


def parse_row(row: Any) -> dict[str, Any] | None:
    viewing_id = row.get("data-viewing-id")
    if not viewing_id:
        return None

    # Film identity lives on the LazyPoster react-component div.
    poster = row.select_one("[data-item-name]")
    if poster is None:
        return None

    name_year = poster.get("data-item-name") or ""
    m = NAME_YEAR_RE.match(name_year.strip())
    if m:
        title = m.group("title").strip()
        year = int(m.group("year"))
    else:
        # Films without a year on Letterboxd are uncommon; skip them rather than guess.
        return None

    slug_attr = clean_text(poster.get("data-item-slug")) or ""
    item_link = clean_text(poster.get("data-item-link")) or f"/film/{slug_attr}/"
    letterboxd_url = f"https://letterboxd.com{item_link}"

    # Watched date: <a class="daydate" href="/{user}/diary/films/for/YYYY/MM/DD/">
    daydate = row.select_one("a.daydate")
    watched_date: str | None = None
    if daydate is not None:
        href = daydate.get("href") or ""
        dm = DATE_HREF_RE.search(href)
        if dm:
            watched_date = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
    if watched_date is None:
        return None

    # Rating: <span class="rating ... rated-N"> where N is 1..10 (half-star * 2).
    rating: float | None = None
    rating_el = row.select_one(".rating")
    if rating_el is not None:
        cls = " ".join(rating_el.get("class") or [])
        rm = RATED_CLASS_RE.search(cls)
        if rm:
            n = int(rm.group(1))
            if 1 <= n <= 10:
                rating = n / 2.0

    # Rewatch: <td class="... td-rewatch ..."> with icon-status-on or icon-status-off.
    rewatch = False
    rewatch_td = row.select_one(".td-rewatch, [class*='rewatch']")
    if rewatch_td is not None:
        cls = " ".join(rewatch_td.get("class") or [])
        if "icon-status-on" in cls or "rewatched" in cls:
            rewatch = True

    return {
        "id": f"letterboxd-watch-{viewing_id}",
        "title": title,
        "year": year,
        "watched_date": watched_date,
        "rating": rating,
        "rewatch": rewatch,
        "review_html": None,
        "tmdb_id": None,
        "letterboxd_url": letterboxd_url,
        "slug": movie_slug(title, year),
    }


def fetch_page(session: requests.Session, username: str, page: int) -> list[dict[str, Any]]:
    r = session.get(diary_url(username, page), timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("tr.diary-entry-row")
    out: list[dict[str, Any]] = []
    for row in rows:
        movie = parse_row(row)
        if movie is not None:
            out.append(movie)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape Letterboxd diary into movies.json")
    ap.add_argument(
        "--username",
        default=os.environ.get("LETTERBOXD_USERNAME"),
        help="Letterboxd username (env: LETTERBOXD_USERNAME)",
    )
    ap.add_argument("--delay", type=float, default=1.5, help="Seconds between page requests")
    ap.add_argument("--max-pages", type=int, default=200, help="Safety cap")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    args = ap.parse_args()

    if not args.username:
        print("error: LETTERBOXD_USERNAME not set", file=sys.stderr)
        return 2

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": args.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    movies: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for page in range(1, args.max_pages + 1):
        rows = fetch_page(session, args.username, page)
        if not rows:
            print(f"page {page}: empty, stopping", file=sys.stderr)
            break
        new_count = 0
        for m in rows:
            if m["id"] in seen_ids:
                continue
            seen_ids.add(m["id"])
            movies.append(m)
            new_count += 1
        print(f"page {page}: {len(rows)} rows ({new_count} new, {len(movies)} total)", file=sys.stderr)
        if new_count == 0:
            # Saw a full page of duplicates; we've wrapped around (unlikely) or pagination is broken.
            break
        if args.delay > 0:
            time.sleep(args.delay)

    movies.sort(key=lambda m: (m["watched_date"], m["id"]), reverse=True)
    validate(movies, MOVIE_FIELDS)

    new_bytes = canonical_bytes(movies)
    old_bytes = MOVIES_PATH.read_bytes() if MOVIES_PATH.exists() else b""
    if new_bytes == old_bytes:
        print(f"letterboxd-scrape: no changes ({len(movies)} movies)")
        return 0

    atomic_write_json(MOVIES_PATH, movies)
    print(f"letterboxd-scrape: wrote {len(movies)} movies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
