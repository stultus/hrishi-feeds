"""One-time seeder: convert Letterboxd / Goodreads CSV exports into the canonical JSON schema.

Usage:
    uv run scripts/seed_from_csv.py letterboxd path/to/letterboxd-export.zip
    uv run scripts/seed_from_csv.py goodreads path/to/goodreads_library_export.csv

The Letterboxd export is a ZIP containing diary.csv (preferred) or watched.csv + ratings.csv +
reviews.csv. We read diary.csv directly because it carries Watched Date, Rating, and Rewatch
together. The Goodreads export is a single CSV; we keep rows where 'Exclusive Shelf' == 'read'.

This script overwrites data/movies.json or data/books.json. Subsequent RSS polls will upsert on top.
Run only when bootstrapping the repo from historical data.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import zipfile
from typing import Any

from dateutil import parser as dateparser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    BOOK_FIELDS,
    DATA_DIR,
    MOVIE_FIELDS,
    atomic_write_json,
    book_slug,
    clean_text,
    movie_slug,
    validate,
)


def _date(raw: str | None) -> str | None:
    s = clean_text(raw)
    if not s:
        return None
    try:
        return dateparser.parse(s).date().isoformat()
    except (ValueError, TypeError, dateparser.ParserError):
        return None


def _letterboxd_rating(raw: str | None) -> float | None:
    s = clean_text(raw)
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _bool(raw: str | None) -> bool:
    return clean_text(raw or "").lower() in ("yes", "true", "1")


def seed_letterboxd(zip_path: str) -> list[dict[str, Any]]:
    movies: list[dict[str, Any]] = []
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        diary_name = next((n for n in names if n.endswith("diary.csv")), None)
        if diary_name is None:
            raise SystemExit(
                "diary.csv not found in export zip; cannot seed without watched dates"
            )
        with zf.open(diary_name) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))
            for row in reader:
                title = clean_text(row.get("Name"))
                year_raw = clean_text(row.get("Year"))
                uri = clean_text(row.get("Letterboxd URI"))
                watched = _date(row.get("Watched Date") or row.get("Date"))
                if not (title and year_raw and uri and watched):
                    continue
                try:
                    year = int(year_raw)
                except ValueError:
                    continue
                movies.append(
                    {
                        "id": uri,
                        "title": title,
                        "year": year,
                        "watched_date": watched,
                        "rating": _letterboxd_rating(row.get("Rating")),
                        "rewatch": _bool(row.get("Rewatch")),
                        "review_html": None,
                        "tmdb_id": None,
                        "letterboxd_url": uri,
                        "slug": movie_slug(title, year),
                    }
                )

        # Optional: enrich with reviews.csv (review HTML lives there).
        reviews_name = next((n for n in names if n.endswith("reviews.csv")), None)
        if reviews_name is not None:
            review_by_uri: dict[str, str] = {}
            with zf.open(reviews_name) as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))
                for row in reader:
                    uri = clean_text(row.get("Letterboxd URI"))
                    review = clean_text(row.get("Review"))
                    if uri and review:
                        review_by_uri[uri] = review
            for m in movies:
                if m["id"] in review_by_uri:
                    m["review_html"] = review_by_uri[m["id"]]

    movies.sort(key=lambda m: (m["watched_date"], m["id"]), reverse=True)
    validate(movies, MOVIE_FIELDS)
    return movies


def _gr_rating(raw: str | None) -> int | None:
    s = clean_text(raw)
    if not s:
        return None
    try:
        v = int(s)
    except ValueError:
        return None
    return v if 1 <= v <= 5 else None


def seed_goodreads(csv_path: str) -> list[dict[str, Any]]:
    books: list[dict[str, Any]] = []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            shelf = clean_text(row.get("Exclusive Shelf")) or ""
            if shelf != "read":
                continue
            book_id = clean_text(row.get("Book Id"))
            title = clean_text(row.get("Title"))
            author = clean_text(row.get("Author")) or ""
            added = _date(row.get("Date Added"))
            if not (book_id and title and added):
                continue
            isbn = clean_text(row.get("ISBN13")) or clean_text(row.get("ISBN"))
            # Goodreads CSV wraps ISBNs as ="978...". Strip the formula wrapper.
            if isbn:
                isbn = isbn.strip("=").strip('"') or None
            books.append(
                {
                    "id": book_id,
                    "title": title,
                    "author": author,
                    "read_date": _date(row.get("Date Read")),
                    "added_date": added,
                    "rating": _gr_rating(row.get("My Rating")),
                    "review_html": clean_text(row.get("My Review")),
                    "isbn": isbn,
                    "cover_url": None,
                    "goodreads_url": f"https://www.goodreads.com/book/show/{book_id}",
                    "slug": book_slug(title, author),
                }
            )

    def _neg(s: str | None) -> str:
        if s is None:
            return ""
        return "".join(chr(0x7E - ord(c)) for c in s)

    books.sort(
        key=lambda b: (
            b["read_date"] is None,
            _neg(b["read_date"]),
            _neg(b["added_date"]),
            b["id"],
        )
    )
    validate(books, BOOK_FIELDS)
    return books


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed data/*.json from Letterboxd/Goodreads CSV exports")
    ap.add_argument("source", choices=["letterboxd", "goodreads"])
    ap.add_argument("path", help="Path to export ZIP (Letterboxd) or CSV (Goodreads)")
    args = ap.parse_args()

    if args.source == "letterboxd":
        items = seed_letterboxd(args.path)
        out = DATA_DIR / "movies.json"
    else:
        items = seed_goodreads(args.path)
        out = DATA_DIR / "books.json"

    atomic_write_json(out, items)
    print(f"seeded {out} with {len(items)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
