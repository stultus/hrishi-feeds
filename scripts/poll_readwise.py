"""Mirror the Readwise Books "finished" shelf into data/books.json.

Readwise is the source of truth for which books are finished, when, and how
they're rated. Entries are linked to Readwise by `readwise_id`, and the
fields that are about presentation (title, author, slug, cover, link) are
kept once an entry exists, so hand-picked covers and links survive syncs and
book page URLs stay stable. Each run fetches the whole shelf, because Readwise
doesn't bump `updated` when a rating changes; that's one request for up to
1000 books.

Run with --dry-run to print the diff without writing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    BOOK_FIELDS,
    DATA_DIR,
    atomic_write_json,
    book_slug,
    canonical_bytes,
    load_json,
    validate,
)

BOOKS_PATH = DATA_DIR / "books.json"
API = "https://readwise.io/books/api/v1"

# Readwise stores reading dates as instants; Kindle ones are local midnight
# (e.g. 18:30Z for IST), so take the calendar date in this zone.
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# Refuse to write if the shelf shrinks by more than this, to guard against a
# bad API response wiping the page.
MAX_DROP = 0.2


def fetch_finished(token: str) -> list[dict[str, Any]]:
    url = f"{API}/books?" + urllib.parse.urlencode(
        {"status": "finished", "limit": 1000, "order": "created"}
    )
    books: list[dict[str, Any]] = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Token {token}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            page = json.load(r)
        books.extend(page["results"])
        url = page["next"]
    return books


def display_author(author: str | None) -> str:
    """Goodreads imports store a single author as "Last, First"; flip those."""
    author = (author or "").strip()
    if author.count(",") == 1:
        last, first = (p.strip() for p in author.split(","))
        if first and last:
            return f"{first} {last}"
    return author


def date_part(ts: str | None) -> str | None:
    if not ts:
        return None
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(LOCAL_TZ).date().isoformat()


def store_link(product: dict[str, Any]) -> str | None:
    if product.get("store_url"):
        return product["store_url"]
    if product.get("asin"):
        return f"https://www.amazon.in/dp/{product['asin']}"
    return None


def merge(existing: list[dict[str, Any]], shelf: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_rw = {b["readwise_id"]: dict(b) for b in existing if b["readwise_id"] is not None}
    slugs = {b["slug"] for b in existing}
    merged = [dict(b) for b in existing if b["readwise_id"] is None]

    for rw in shelf:
        product = rw["product"] or {}
        rating = rw["rating"]
        rating = int(rating) if rating is not None and rating == int(rating) else rating
        book = by_rw.get(rw["id"])
        if book is None:
            title = rw["title"] or product.get("title") or ""
            author = display_author(rw["author"] or product.get("author"))
            slug, n = book_slug(title, author), 2
            while slug in slugs:
                slug, n = f"{book_slug(title, author)}-{n}", n + 1
            slugs.add(slug)
            book = {
                "id": f"rw-{rw['id']}",
                "readwise_id": rw["id"],
                "title": title,
                "author": author,
                "added_date": date_part(rw["finished_reading_at"] or rw["created"]),
                "review_html": None,
                "isbn": None,
                "cover_url": None,
                "goodreads_url": store_link(product),
                "slug": slug,
            }
        book["read_date"] = date_part(rw["finished_reading_at"]) or book.get("read_date")
        book["rating"] = rating
        book["isbn"] = book["isbn"] or product.get("isbn")
        book["cover_url"] = book["cover_url"] or product.get("cover_url")
        merged.append(book)
    return merged


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
    ap = argparse.ArgumentParser(description="Mirror Readwise Books' finished shelf into books.json")
    ap.add_argument("--dry-run", action="store_true", help="Print diff instead of writing")
    args = ap.parse_args()

    token = os.environ.get("READWISE_TOKEN")
    if not token:
        print("error: READWISE_TOKEN must be set", file=sys.stderr)
        return 2

    existing = load_json(BOOKS_PATH)
    shelf = fetch_finished(token)
    if len(shelf) < len(existing) * (1 - MAX_DROP):
        raise RuntimeError(
            f"Readwise returned {len(shelf)} finished books but books.json has "
            f"{len(existing)}; refusing to drop that many"
        )

    merged = sort_books(merge(existing, shelf))
    validate(merged, BOOK_FIELDS)

    new_bytes = canonical_bytes(merged)
    old_bytes = BOOKS_PATH.read_bytes() if BOOKS_PATH.exists() else b""

    if new_bytes == old_bytes:
        print(f"readwise: no changes ({len(merged)} books)")
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
            f"\nreadwise: dry-run, would write {len(merged)} books (was {len(existing)})",
            file=sys.stderr,
        )
        return 0

    atomic_write_json(BOOKS_PATH, merged)
    print(f"readwise: wrote {len(merged)} books (was {len(existing)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
