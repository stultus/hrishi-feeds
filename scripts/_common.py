"""Shared helpers for feed pollers: schema validation, atomic write, slug, JSON IO."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from slugify import slugify as _slugify

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

MOVIE_FIELDS = {
    "id": str,
    "title": str,
    "year": int,
    "watched_date": str,
    "rating": (float, type(None)),
    "rewatch": bool,
    "review_html": (str, type(None)),
    "tmdb_id": (str, type(None)),
    "letterboxd_url": str,
    "slug": str,
}

BOOK_FIELDS = {
    "id": str,
    "title": str,
    "author": str,
    "read_date": (str, type(None)),
    "added_date": str,
    "rating": (int, type(None)),
    "review_html": (str, type(None)),
    "isbn": (str, type(None)),
    "cover_url": (str, type(None)),
    "goodreads_url": (str, type(None)),
    "slug": str,
}


def slugify(text: str) -> str:
    """ASCII-fold, lowercase, strip punctuation, hyphenate."""
    return _slugify(text, lowercase=True, separator="-")


def movie_slug(title: str, year: int | None) -> str:
    base = title if year is None else f"{title} {year}"
    return slugify(base)


def book_slug(title: str, author: str) -> str:
    last = author.strip().split()[-1] if author.strip() else ""
    return slugify(f"{title} {last}")


def load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected JSON array")
    return data


def atomic_write_json(path: Path, items: list[dict[str, Any]]) -> None:
    """Write list as pretty JSON via tempfile + os.replace in same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(items, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialized)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def canonical_bytes(items: list[dict[str, Any]]) -> bytes:
    """Stable serialization used for diffing — same as atomic_write_json."""
    return (
        json.dumps(items, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def validate(items: list[dict[str, Any]], schema: dict[str, Any]) -> None:
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"item {i}: not an object")
        missing = set(schema) - set(item)
        if missing:
            raise ValueError(f"item {i} ({item.get('id')}): missing fields {missing}")
        extra = set(item) - set(schema)
        if extra:
            raise ValueError(f"item {i} ({item.get('id')}): unexpected fields {extra}")
        for field, expected in schema.items():
            value = item[field]
            allowed = expected if isinstance(expected, tuple) else (expected,)
            # bool is a subclass of int; reject when int is not in allowed.
            if isinstance(value, bool) and bool not in allowed:
                raise ValueError(
                    f"item {i} ({item.get('id')}): {field} got bool, expected {allowed}"
                )
            if not isinstance(value, allowed):
                raise ValueError(
                    f"item {i} ({item.get('id')}): {field} got {type(value).__name__}, expected {allowed}"
                )


def merge_by_id(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Upsert: incoming entries replace existing ones with the same id; historical entries are preserved."""
    by_id: dict[str, dict[str, Any]] = {item["id"]: item for item in existing}
    for item in incoming:
        by_id[item["id"]] = item
    return list(by_id.values())


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_iso_date(s: str | None) -> bool:
    return bool(s) and bool(_DATE_RE.match(s))


def clean_text(s: str | None) -> str | None:
    if s is None:
        return None
    s = unicodedata.normalize("NFC", s).strip()
    return s or None
