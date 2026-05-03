"""Poll Goodreads 'read' shelf RSS and merge entries into data/books.json.

Run with --dry-run to print the diff without writing.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from typing import Any

import feedparser
from dateutil import parser as dateparser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    BOOK_FIELDS,
    DATA_DIR,
    atomic_write_json,
    book_slug,
    canonical_bytes,
    clean_text,
    load_json,
    merge_by_id,
    validate,
)

BOOKS_PATH = DATA_DIR / "books.json"


def feed_url(user_id: str, key: str, shelf: str = "read") -> str:
    return (
        f"https://www.goodreads.com/review/list_rss/{user_id}"
        f"?key={key}&shelf={shelf}"
    )


def parse_date(raw: Any) -> str | None:
    s = clean_text(str(raw) if raw is not None else None)
    if not s:
        return None
    try:
        dt = dateparser.parse(s)
    except (ValueError, TypeError, dateparser.ParserError):
        return None
    if dt is None:
        return None
    return dt.date().isoformat()


def parse_rating(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if v <= 0 or v > 5:
        return None
    return v


def entry_to_book(entry: dict[str, Any]) -> dict[str, Any] | None:
    book_id = clean_text(entry.get("book_id")) or clean_text(entry.get("id"))
    if not book_id:
        return None

    title = clean_text(entry.get("title"))
    if not title:
        return None

    author = clean_text(entry.get("author_name")) or ""

    added_date = parse_date(entry.get("user_date_added")) or parse_date(
        entry.get("user_date_created")
    ) or parse_date(entry.get("published"))
    if added_date is None:
        return None

    cover_url = (
        clean_text(entry.get("book_large_image_url"))
        or clean_text(entry.get("book_medium_image_url"))
        or clean_text(entry.get("book_image_url"))
        or clean_text(entry.get("book_small_image_url"))
    )

    review_html = clean_text(entry.get("user_review")) or None
    isbn = clean_text(entry.get("isbn")) or None

    return {
        "id": book_id,
        "title": title,
        "author": author,
        "read_date": parse_date(entry.get("user_read_at")),
        "added_date": added_date,
        "rating": parse_rating(entry.get("user_rating")),
        "review_html": review_html,
        "isbn": isbn,
        "cover_url": cover_url,
        "goodreads_url": clean_text(entry.get("link")) or "",
        "slug": book_slug(title, author),
    }


def sort_books(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # read_date desc with nulls last, then added_date desc, then id for stability.
    return sorted(
        items,
        key=lambda b: (
            b["read_date"] is None,  # False (has date) sorts before True (null)
            # Sort dates descending: invert by negating with a sentinel.
            # Use empty string for nulls so they don't break the comparison.
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


def fetch(user_id: str, key: str) -> list[dict[str, Any]]:
    parsed = feedparser.parse(feed_url(user_id, key))
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"Goodreads feed parse failed: {parsed.bozo_exception}")
    books: list[dict[str, Any]] = []
    for entry in parsed.entries:
        book = entry_to_book(entry)
        if book is not None:
            books.append(book)
    return books


def main() -> int:
    ap = argparse.ArgumentParser(description="Poll Goodreads RSS into books.json")
    ap.add_argument("--dry-run", action="store_true", help="Print diff instead of writing")
    ap.add_argument(
        "--user-id",
        default=os.environ.get("GOODREADS_USER_ID"),
        help="Goodreads numeric user id (env: GOODREADS_USER_ID)",
    )
    ap.add_argument(
        "--key",
        default=os.environ.get("GOODREADS_RSS_KEY"),
        help="Goodreads RSS key (env: GOODREADS_RSS_KEY)",
    )
    ap.add_argument(
        "--shelf",
        default=os.environ.get("GOODREADS_SHELF", "read"),
        help="Goodreads shelf name (default: read)",
    )
    args = ap.parse_args()

    if not args.user_id or not args.key:
        print("error: GOODREADS_USER_ID and GOODREADS_RSS_KEY must be set", file=sys.stderr)
        return 2

    existing = load_json(BOOKS_PATH)
    incoming = fetch(args.user_id, args.key)
    merged = sort_books(merge_by_id(existing, incoming))
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
