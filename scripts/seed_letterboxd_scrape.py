"""One-time backfill: scrape the public Letterboxd diary into data/movies.json.

Setup (once):
    uv sync
    uv run playwright install chromium

Run:
    LETTERBOXD_USERNAME=stultus uv run scripts/seed_letterboxd_scrape.py

Each diary row carries data-viewing-id="NNNNN", which the RSS feed surfaces as
'letterboxd-watch-NNNNN'. Using `letterboxd-watch-{viewing_id}` as our id keeps
backfill and ongoing RSS polls in the same id space, so subsequent polls
upsert cleanly without producing duplicates.

Why headless Chromium and not requests:
    Letterboxd sits behind Cloudflare with TLS-fingerprint + JS-challenge
    bot defenses. Plain HTTP libraries (requests, httpx) get a 403 regardless
    of User-Agent. Playwright drives a real browser, passes the challenge,
    and returns the rendered HTML.

Fields populated by the scrape: id, title, year, watched_date, rating,
rewatch, slug, letterboxd_url. The diary listing does not expose review_html
or tmdb_id; the nightly RSS poll fills both for the most recent ~50 watches.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Any

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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

NAME_YEAR_RE = re.compile(r"^(?P<title>.+?)\s+\((?P<year>\d{4})\)\s*$")
DATE_HREF_RE = re.compile(r"/diary/films/for/(\d{4})/(\d{2})/(\d{2})/")
RATED_CLASS_RE = re.compile(r"rated-(\d+)")


def diary_url(username: str, page: int) -> str:
    return f"https://letterboxd.com/{username}/films/diary/page/{page}/"


def parse_row(row: Any) -> dict[str, Any] | None:
    viewing_id = row.get("data-viewing-id")
    if not viewing_id:
        return None

    poster = row.select_one("[data-item-name]")
    if poster is None:
        return None

    name_year = poster.get("data-item-name") or ""
    m = NAME_YEAR_RE.match(name_year.strip())
    if m:
        title = m.group("title").strip()
        year = int(m.group("year"))
    else:
        return None

    slug_attr = clean_text(poster.get("data-item-slug")) or ""
    item_link = clean_text(poster.get("data-item-link")) or f"/film/{slug_attr}/"
    letterboxd_url = f"https://letterboxd.com{item_link}"

    daydate = row.select_one("a.daydate")
    watched_date: str | None = None
    if daydate is not None:
        href = daydate.get("href") or ""
        dm = DATE_HREF_RE.search(href)
        if dm:
            watched_date = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
    if watched_date is None:
        return None

    rating: float | None = None
    rating_el = row.select_one(".rating")
    if rating_el is not None:
        cls = " ".join(rating_el.get("class") or [])
        rm = RATED_CLASS_RE.search(cls)
        if rm:
            n = int(rm.group(1))
            if 1 <= n <= 10:
                rating = n / 2.0

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


def parse_html(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
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
    ap.add_argument("--delay", type=float, default=1.5, help="Seconds between page loads")
    ap.add_argument("--max-pages", type=int, default=200, help="Safety cap")
    ap.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser window (useful for debugging Cloudflare prompts)",
    )
    args = ap.parse_args()

    if not args.username:
        print("error: LETTERBOXD_USERNAME not set", file=sys.stderr)
        return 2

    movies: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    with sync_playwright() as pw:
        # The default headless-shell binary is fingerprinted by Cloudflare; pass
        # --headless=new to use full Chromium in headless mode, which clears the
        # "Just a moment..." challenge.
        launch_args = [] if args.headed else ["--headless=new", "--disable-blink-features=AutomationControlled"]
        browser = pw.chromium.launch(headless=False, args=launch_args)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        # Hide the navigator.webdriver flag that Playwright sets by default.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = context.new_page()

        try:
            for page_num in range(1, args.max_pages + 1):
                url = diary_url(args.username, page_num)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except PWTimeout:
                    print(f"page {page_num}: nav timeout, retrying once", file=sys.stderr)
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    except PWTimeout:
                        print(f"page {page_num}: nav timeout twice, stopping", file=sys.stderr)
                        break

                # Wait for the actual diary table OR an "empty" diary marker.
                # Cloudflare interstitial doesn't have either, so this waits past it.
                try:
                    page.wait_for_selector(
                        "tr.diary-entry-row, .empty-text, .no-films",
                        timeout=30000,
                        state="attached",
                    )
                except PWTimeout:
                    snapshot = DATA_DIR.parent / f"debug_page_{page_num}.html"
                    snapshot.write_text(page.content(), encoding="utf-8")
                    print(
                        f"page {page_num}: no diary content after 30s; snapshot at {snapshot}",
                        file=sys.stderr,
                    )
                    break

                html = page.content()
                rows = parse_html(html)
                if not rows:
                    snapshot = DATA_DIR.parent / f"debug_page_{page_num}.html"
                    snapshot.write_text(html, encoding="utf-8")
                    print(
                        f"page {page_num}: parsed 0 rows; snapshot at {snapshot}",
                        file=sys.stderr,
                    )
                    break

                new_count = 0
                for m in rows:
                    if m["id"] in seen_ids:
                        continue
                    seen_ids.add(m["id"])
                    movies.append(m)
                    new_count += 1
                print(
                    f"page {page_num}: {len(rows)} rows ({new_count} new, {len(movies)} total)",
                    file=sys.stderr,
                )
                if new_count == 0:
                    break
                if args.delay > 0:
                    time.sleep(args.delay)
        finally:
            context.close()
            browser.close()

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
