---
name: add-book
description: Add a finished book to the reading list via Readwise Books, with an optional cover (image URL or local file) and link. Use when asked to add, log or record a book by hand, especially one Goodreads or Readwise's catalogue doesn't have.
---

# Add a book

`data/books.json` mirrors the Readwise Books **finished** shelf (see `scripts/poll_readwise.py`). So a book is added by putting it in Readwise, then syncing. Readwise has no fields for a custom cover or link, so those are set on the synced entry afterwards; the poller keeps them from then on.

## 1. Collect the details

Ask only for what's missing from the request:

| Field | Required | Notes |
|---|---|---|
| Title | yes | Keep the original script (e.g. Malayalam) as given |
| Author | yes | |
| Date finished | no | `YYYY-MM-DD`; default today |
| Rating | no | 0.25–5 in quarter steps (e.g. 3.5), or none |
| ISBN | no | ISBN-10 or -13; helps Readwise match its catalogue (cover, store link). An Amazon `/dp/` link for an Indian book often contains the ISBN-10 |
| Link | no | Any page about the book (publisher, store, affiliate link) |
| Cover | no | An image URL, or a path to a local image file |

The Readwise token must be in the environment as `READWISE_TOKEN`. If it isn't set, ask the user to run `! export READWISE_TOKEN=...` (token from https://readwise.io/access_token). Never write the token to a file, commit it, or print it.

## 2. Sync and check for duplicates

```sh
git pull --rebase
```

The poll workflow commits to `main` every hour, so always pull first. Search `data/books.json` for the title and author (case-insensitive, also try a transliteration). If the book is already there, show the user the existing entry and stop unless they confirm.

## 3. Add it to Readwise

Run from the repo root, filling in the values at the top. It prints the result, including the Readwise `book_id`; keep it for the next steps.

```sh
uv run python - <<'PY'
import json, os, urllib.request

title = "TITLE"
author = "AUTHOR"
finished = "YYYY-MM-DD"
rating = None          # or 0.25-5 in quarter steps
isbn = None            # or "978..."

product = {"title": title, "author": author}
if isbn:
    product["isbn"] = isbn
item = {
    "product": product,
    "status": "finished",
    "started_reading_at": f"{finished}T00:00:00Z",
    "finished_reading_at": f"{finished}T00:00:00Z",
    "rating": rating,
}
req = urllib.request.Request(
    "https://readwise.io/books/api/v1/books",
    data=json.dumps({"items": [item]}).encode(),
    headers={"Authorization": f"Token {os.environ['READWISE_TOKEN']}", "Content-Type": "application/json"},
    method="POST",
)
print(json.load(urllib.request.urlopen(req, timeout=30))["results"][0])
PY
```

Check the result:

- `"status": "created"`: added. `matched_by` says whether Readwise found it in its catalogue (`isbn`, `title_author`) or not (`None`).
- `"status": "existing"`: the book was already in the user's Readwise library (perhaps on another shelf). The date, rating and finished status were applied to it. Tell the user.
- `"status": "error"`: show the error and stop.

## 4. Sync it into books.json

```sh
uv run scripts/poll_readwise.py
```

The new entry has `"readwise_id": <book_id>` and `"id": "rw-<book_id>"`. If Readwise matched its catalogue, it may already have a cover, ISBN and store link.

## 5. Set the cover and link (if given)

Covers live in the repo at `data/covers/<id>.<ext>`, using the entry's `id` (e.g. `rw-6723398.jpg`). A URL is downloaded rather than hotlinked, so the cover survives the other site changing; a local file is copied. Both are shrunk to 475px on the long edge with `sips` (macOS) when available, to match the other covers. The repo is public, so the site loads them from `raw.githubusercontent.com`. A cover or link given here replaces whatever Readwise's catalogue supplied.

```sh
uv run python - <<'PY'
import shutil, subprocess, sys, urllib.request
sys.path.insert(0, "scripts")
from pathlib import Path
from _common import BOOK_FIELDS, atomic_write_json, load_json, validate

readwise_id = 0        # book_id from step 3
link = None            # or "https://..."
cover = None           # or "https://..." image URL, or a local file path

path = Path("data/books.json")
books = load_json(path)
book = next(b for b in books if b["readwise_id"] == readwise_id)

if cover:
    ext = Path(cover.split("?")[0]).suffix.lower()
    ext = ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
    dest = Path("data/covers") / f"{book['id']}{ext}"
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
    book["cover_url"] = f"https://raw.githubusercontent.com/stultus/hrishi-feeds/main/{dest.as_posix()}"
if link:
    book["goodreads_url"] = link

validate(books, BOOK_FIELDS)
atomic_write_json(path, books)
print(book)
PY
```

If a cover was stored, open it (e.g. read the image) to check it's the right book and not an error page.

## 6. Review, commit, push

1. `git diff data/books.json` should show exactly one new entry. Show it to the user.
2. Commit `data/books.json` and any cover with a message like `Add <title>`.
3. Push to `main`. If the push is rejected because the poller committed in the meantime, run `git pull --rebase` and push again.

Pushing changes under `data/` triggers `dispatch.yml`, which rebuilds stultus.in and notes-hugo. The book appears on https://stultus.in/books/ within a minute or two.

## Rules

- Change books in Readwise, not by editing `read_date`, `rating` or the book list in `books.json`: the next sync overwrites those from Readwise. Only `cover_url` and `goodreads_url` (the link) are safe to set by hand.
- Never hand-edit `books.json` formatting; always write it with `atomic_write_json` so the poller's byte comparison stays stable.
