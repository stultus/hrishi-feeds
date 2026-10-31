"""Poll Goodreads' public widgets and merge entries into data/books.json.

Goodreads retired its shelf RSS feeds in October 2026, so this reads two
public, signed-out widgets instead:

- The updates widget (the profile's recent activity, last 10 entries) gives
  "has read" and "rated" events with dates and ratings. It is one small
  request, so every run starts here and exits early when there is nothing new.
- The shelf widget (the "read" shelf, newest first) gives review URLs, covers
  and ratings. It is only fetched when the updates widget shows something new,
  or with --full, which also catches books the updates widget missed.

Run with --dry-run to print the diff without writing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import os
import re
import sys
import urllib.request
from typing import Any

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    BOOK_FIELDS,
    DATA_DIR,
    atomic_write_json,
    book_slug,
    canonical_bytes,
    clean_text,
    load_json,
    validate,
)

BOOKS_PATH = DATA_DIR / "books.json"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Goodreads' star labels, as used in "rated a book <label>".
RATING_LABELS = {
    "did not like it": 1,
    "it was ok": 2,
    "liked it": 3,
    "really liked it": 4,
    "it was amazing": 5,
}

BOOK_ID_RE = re.compile(r"/book/show/(\d+)")
COVER_ID_RE = re.compile(r"/(\d+)\.[^/]*$")
COVER_SIZE_RE = re.compile(r"\._S[XY]\d+_(?=\.)")
REVIEW_ID_RE = re.compile(r"/review/show/(\d+)")
MONTH_DAY_RE = re.compile(r"^[A-Z][a-z]+ \d{1,2}$")


def updates_url(user_id: str) -> str:
    return (
        "https://www.goodreads.com/widgets/user_update_widget"
        f"?user={user_id}&num_updates=10&width=250&height=400"
    )


def shelf_url(user_id: str, shelf: str, num_books: int) -> str:
    # widget_id busts Goodreads' per-widget cache.
    return (
        f"https://www.goodreads.com/review/custom_widget/{user_id}"
        f"?shelf={shelf}&sort=date_read&order=d&num_books={num_books}"
        "&show_cover=1&show_title=1&show_author=1&show_rating=1"
        f"&widget_id={int(dt.datetime.now().timestamp())}"
    )


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def numeric_user_id(user_id: str) -> str:
    # The shelf widget 404s on "123-name" slugs; accept either form.
    return user_id.split("-", 1)[0]


def full_size_cover(url: str | None) -> str | None:
    """Swap the widget's thumbnail size for the 475px height books.json uses."""
    if not url or "nophoto" in url:
        return None
    return COVER_SIZE_RE.sub("._SY475_", url)


def infer_date(month_day: str, today: dt.date) -> str | None:
    """'May 24' -> ISO date, in the most recent year that isn't in the future."""
    try:
        parsed = dt.datetime.strptime(f"{month_day} {today.year}", "%B %d %Y").date()
    except ValueError:
        try:
            parsed = dt.datetime.strptime(f"{month_day} {today.year}", "%b %d %Y").date()
        except ValueError:
            return None
    if parsed > today:
        parsed = parsed.replace(year=today.year - 1)
    return parsed.isoformat()


def parse_updates(html: str, today: dt.date) -> list[dict[str, Any]]:
    """Return read/rated events, newest first, each with a book id and date.

    Only the first update of each day carries a date; later ones inherit it.
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []
    current_date = today.isoformat()
    for update in soup.select("div.update"):
        date_p = update.find("p", recursive=False)
        if date_p and MONTH_DAY_RE.match(date_p.get_text(strip=True)):
            current_date = infer_date(date_p.get_text(strip=True), today) or current_date

        text = " ".join(update.get_text(" ", strip=True).split())
        title_a = update.find("a", class_="bookTitle")
        if not title_a:
            continue
        m = BOOK_ID_RE.search(title_a.get("href", ""))
        if not m:
            continue

        rating = None
        if "has read" in text:
            kind = "read"
        elif "rated a book" in text:
            kind = "rated"
            label = text.split("rated a book", 1)[1]
            # Longest label first so "really liked it" doesn't match "liked it".
            for name in sorted(RATING_LABELS, key=len, reverse=True):
                if label.strip().startswith(name):
                    rating = RATING_LABELS[name]
                    break
        else:
            continue  # likes, progress, want-to-read, etc.

        author_a = update.find("a", class_="authorName")
        img = update.find("img")
        events.append(
            {
                "kind": kind,
                "book_id": m.group(1),
                "title": clean_text(title_a.get_text()),
                "author": clean_text(author_a.get_text()) if author_a else "",
                "date": current_date,
                "rating": rating,
                "cover_url": full_size_cover(img.get("src")) if img else None,
            }
        )
    return events


def parse_shelf(js: str) -> list[dict[str, Any]]:
    """Parse the shelf widget's `var widget_code = '...'` JavaScript payload."""
    m = re.search(r"var widget_code = '(.*?)'\s*var widget_div", js, re.S)
    if not m:
        raise RuntimeError("Goodreads shelf widget: widget_code not found")
    html = (
        m.group(1)
        .replace("\\/", "/")
        .replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\'", "'")
    )
    soup = BeautifulSoup(html, "html.parser")
    books: list[dict[str, Any]] = []
    for each in soup.select("div[class^=gr_custom_each_container]"):
        img = each.find("img")
        cover = img.get("src") if img else None
        book_m = COVER_ID_RE.search(cover or "") if cover and "nophoto" not in cover else None
        title_div = each.select_one("div[class^=gr_custom_title]")
        author_div = each.select_one("div[class^=gr_custom_author]")
        review_a = title_div.find("a") if title_div else None
        # The cover link's title attribute has the full title; the visible
        # text drops suffixes like "(Malayalam Edition)".
        cover_a = img.find_parent("a") if img else None
        title = clean_text(cover_a.get("title")) if cover_a else None
        review_m = REVIEW_ID_RE.search(review_a.get("href", "")) if review_a else None
        stars = each.select("span.staticStars img")
        rating = sum(1 for s in stars if "_active" in s.get("src", "")) or None
        books.append(
            {
                "book_id": book_m.group(1) if book_m else None,
                "review_id": review_m.group(1) if review_m else None,
                "title": title or (clean_text(review_a.get_text()) if review_a else None),
                "author": clean_text(author_div.find("a").get_text())
                if author_div and author_div.find("a")
                else "",
                "rating": rating,
                "cover_url": full_size_cover(cover),
            }
        )
    return books


def review_url(review_id: str) -> str:
    return f"https://www.goodreads.com/review/show/{review_id}?utm_medium=api&utm_source=rss"


def has_new_updates(events: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]) -> bool:
    for e in events:
        known = by_id.get(e["book_id"])
        if known is None:
            return True
        if e["rating"] is not None and e["rating"] != known["rating"]:
            return True
    return False


def merge(
    existing: list[dict[str, Any]],
    events: list[dict[str, Any]],
    shelf: list[dict[str, Any]],
    today: dt.date,
) -> list[dict[str, Any]]:
    by_id = {b["id"]: dict(b) for b in existing}
    by_review = {}
    for b in by_id.values():
        m = REVIEW_ID_RE.search(b.get("goodreads_url") or "")
        if m:
            by_review[m.group(1)] = b["id"]

    # Fold events per book, oldest first, so the newest rating wins. A "has
    # read" date beats a "rated" date; a book that was only rated uses that.
    event_by_book: dict[str, dict[str, Any]] = {}
    for e in reversed(events):
        folded = event_by_book.get(e["book_id"])
        if folded is None:
            event_by_book[e["book_id"]] = dict(e)
            continue
        if e["kind"] == "read":
            folded["kind"], folded["date"] = "read", e["date"]
        if e["rating"] is not None:
            folded["rating"] = e["rating"]

    def upsert(book_id: str, src: dict[str, Any], review_id: str | None) -> None:
        event = event_by_book.get(book_id, {})
        rating = event.get("rating") or src.get("rating")
        book = by_id.get(book_id)
        if book is None:
            date = event.get("date") or today.isoformat()
            title = src["title"] or event.get("title")
            author = src.get("author") or event.get("author") or ""
            by_id[book_id] = {
                "id": book_id,
                "title": title,
                "author": author,
                "read_date": date,
                "added_date": date,
                "rating": rating,
                "review_html": None,
                "isbn": None,
                "cover_url": src.get("cover_url") or event.get("cover_url"),
                "goodreads_url": review_url(review_id)
                if review_id
                else f"https://www.goodreads.com/book/show/{book_id}",
                "slug": book_slug(title, author),
            }
            return
        # Existing books keep their data; only fill gaps and take new ratings.
        if rating is not None:
            book["rating"] = rating
        if book["read_date"] is None and event.get("date"):
            book["read_date"] = event["date"]
        if not book["cover_url"] and src.get("cover_url"):
            book["cover_url"] = src["cover_url"]

    for s in shelf:
        book_id = s["book_id"] or by_review.get(s["review_id"] or "")
        if not book_id or not s["title"]:
            print(f"goodreads: skipping shelf entry without a book id: {s['title']}", file=sys.stderr)
            continue
        upsert(book_id, s, s["review_id"])

    # Events for books the shelf widget didn't list (e.g. past its num_books).
    for book_id, e in event_by_book.items():
        if book_id not in by_id:
            upsert(book_id, e, None)
        else:
            upsert(book_id, {}, None)

    return list(by_id.values())


def sort_books(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # read_date desc with nulls last, then added_date desc, then id for stability.
    return sorted(
        items,
        key=lambda b: (
            b["read_date"] is None,  # False (has date) sorts before True (null)
            _neg_key(b["read_date"]),
            _neg_key(b["added_date"]),
            b["id"],
        ),
    )


def _neg_key(date: str | None) -> str:
    """Return a string that sorts in reverse-chronological order for ISO dates."""
    if date is None:
        return ""
    # Each ISO date char is in [0-9-]; complement to invert sort order.
    return "".join(chr(0x7E - ord(c)) for c in date)


def main() -> int:
    ap = argparse.ArgumentParser(description="Poll Goodreads widgets into books.json")
    ap.add_argument("--dry-run", action="store_true", help="Print diff instead of writing")
    ap.add_argument(
        "--full",
        action="store_true",
        help="Always fetch the shelf widget, even when the updates widget shows nothing new",
    )
    ap.add_argument(
        "--user-id",
        default=os.environ.get("GOODREADS_USER_ID"),
        help="Goodreads numeric user id (env: GOODREADS_USER_ID)",
    )
    ap.add_argument(
        "--shelf",
        default=os.environ.get("GOODREADS_SHELF", "read"),
        help="Goodreads shelf name (default: read)",
    )
    ap.add_argument("--num-books", type=int, default=30, help="Shelf widget size (default: 30)")
    args = ap.parse_args()

    if not args.user_id:
        print("error: GOODREADS_USER_ID must be set", file=sys.stderr)
        return 2

    user_id = numeric_user_id(args.user_id)
    today = dt.datetime.now(dt.timezone.utc).date()
    existing = load_json(BOOKS_PATH)
    by_id = {b["id"]: b for b in existing}

    events = parse_updates(get(updates_url(user_id)), today)
    if not has_new_updates(events, by_id) and not args.full:
        print(f"goodreads: no new updates ({len(existing)} books)")
        return 0

    shelf = parse_shelf(get(shelf_url(user_id, args.shelf, args.num_books)))
    if not shelf:
        raise RuntimeError("Goodreads shelf widget returned no books; refusing to continue")

    merged = sort_books(merge(existing, events, shelf, today))
    validate(merged, BOOK_FIELDS)

    new_bytes = canonical_bytes(merged)
    old_bytes = BOOKS_PATH.read_bytes() if BOOKS_PATH.exists() else b""

    if new_bytes == old_bytes:
        print(f"goodreads: no changes ({len(merged)} books)")
        return 0

    if args.dry_run:
        diff = difflib.unified_diff(
            old_bytes.decode("utf-8").splitlines(keepends=True),
            new_bytes.decode("utf-8").splitlines(keepends=True),
            fromfile=str(BOOKS_PATH),
            tofile=str(BOOKS_PATH) + " (new)",
        )
        sys.stdout.writelines(diff)
        print(
            f"\ngoodreads: dry-run, would write {len(merged)} books"
            f" (was {len(existing)})",
            file=sys.stderr,
        )
        return 0

    atomic_write_json(BOOKS_PATH, merged)
    print(f"goodreads: wrote {len(merged)} books (was {len(existing)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
