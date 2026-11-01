---
name: add-book
description: Manually add a book that isn't on Goodreads to data/books.json, with an optional cover from a URL or a local image file. Use when asked to add, log or record a book by hand.
---

# Add a book manually

Some books aren't on Goodreads, so the poller can never pick them up. This adds them straight to `data/books.json`. The poller never deletes or rewrites entries whose `id` it doesn't fetch from Goodreads, so manual entries are safe across syncs as long as their `id` starts with `manual-`.

## 1. Collect the details

Ask only for what's missing from the request:

| Field | Required | Notes |
|---|---|---|
| Title | yes | Keep the original script (e.g. Malayalam) as given |
| Author | yes | |
| Date read | no | `YYYY-MM-DD`; default today |
| Rating | no | Integer 1–5, or none |
| Link | no | Any page about the book (publisher, Wikipedia, store); else `null` |
| Cover | no | An image URL, or a path to a local image file |

## 2. Sync and check for duplicates

```sh
git pull --rebase
```

The poll workflow commits to `main` every hour, so always pull first.

Search `data/books.json` for the title and author (case-insensitive, also try a transliteration). If the book is already there, show the user the existing entry and stop unless they confirm.

## 3. Add the entry

Run from the repo root, filling in the values at the top. The script:

- works out `slug` (title + author's last name, ASCII-folded, via `book_slug`) and `id` (`manual-<slug>`, with `-2`, `-3`… if taken);
- stores the cover in the repo at `data/covers/<id>.<ext>`. A URL is downloaded rather than hotlinked, so the cover survives the other site changing. A local file is copied. Both are shrunk to 475px on the long edge with `sips` (macOS) when available, to match Goodreads covers. The repo is public, so the site loads the cover from `raw.githubusercontent.com`;
- adds the book, sorts the list the way the poller does, validates every entry and writes the file in the canonical format.

```sh
uv run python - <<'PY'
import shutil, subprocess, sys, urllib.request
sys.path.insert(0, "scripts")
from pathlib import Path
from _common import BOOK_FIELDS, atomic_write_json, book_slug, load_json, validate
from poll_goodreads import sort_books

title = "TITLE"
author = "AUTHOR"
read_date = "YYYY-MM-DD"
rating = None          # or 1-5
link = None            # or "https://..."
cover = None           # or "https://..." image URL, or a local file path

path = Path("data/books.json")
books = load_json(path)
slug = book_slug(title, author)
ids = {b["id"] for b in books}
book_id, n = f"manual-{slug}", 2
while book_id in ids:
    book_id, n = f"manual-{slug}-{n}", n + 1

cover_url = None
if cover:
    ext = Path(cover.split("?")[0]).suffix.lower()
    ext = ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
    dest = Path("data/covers") / f"{book_id}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if cover.startswith(("http://", "https://")):
        req = urllib.request.Request(cover, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if not r.headers.get("Content-Type", "").startswith("image/"):
                sys.exit(f"not an image: {r.headers.get('Content-Type')}")
            dest.write_bytes(r.read())
    else:
        shutil.copyfile(Path(cover).expanduser(), dest)
    if shutil.which("sips"):
        subprocess.run(["sips", "-Z", "475", str(dest)], check=True, capture_output=True)
    elif dest.stat().st_size > 300_000:
        print(f"warning: {dest} is {dest.stat().st_size // 1000} KB and wasn't resized")
    cover_url = f"https://raw.githubusercontent.com/stultus/hrishi-feeds/main/{dest.as_posix()}"

books.append({
    "id": book_id,
    "title": title,
    "author": author,
    "read_date": read_date,
    "added_date": read_date,
    "rating": rating,
    "review_html": None,
    "isbn": None,
    "cover_url": cover_url,
    "goodreads_url": link,
    "slug": slug,
})
books = sort_books(books)
validate(books, BOOK_FIELDS)
atomic_write_json(path, books)
print(f"added {book_id} ({len(books)} books)" + (f", cover {cover_url}" if cover_url else ""))
PY
```

If a cover was stored, open it (e.g. read the image) to check it's the right book and not an error page.

## 4. Review, commit, push

1. `git diff data/books.json` should show exactly one new entry. Show it to the user.
2. Commit `data/books.json` and any cover with a message like `Add <title> manually`.
3. Push to `main`. If the push is rejected because the poller committed in the meantime, run `git pull --rebase` and push again.

Pushing changes under `data/` triggers `dispatch.yml`, which rebuilds stultus.in. Give the user a heads-up that the book will appear on https://stultus.in/books/ in a few minutes. If the book is later added to Goodreads, the poller will add it as a separate entry, so the manual one should then be deleted.

## Rules

- Never edit or remove entries that don't start with `manual-` as part of this skill.
- Never hand-edit `books.json` formatting; always write it with the step 3 script so the poller's byte comparison stays stable.
