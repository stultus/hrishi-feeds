# hrishi-feeds

Personal data ingestion. Polls Letterboxd (RSS) and Readwise Books (API) on a schedule, normalizes them
to versioned JSON in `data/`, and triggers downstream Hugo rebuilds via `repository_dispatch`.

The repo is single-purpose: scheduled poll → diff data files → commit on real change → notify
consumers (`stultus/stultus.in`, `stultus/notes-hugo`).

## Layout

```
data/
  movies.json          # array, sorted by watched_date desc
  books.json           # array, sorted by read_date desc (nulls last)
scripts/
  poll_letterboxd.py   # hourly poll
  poll_readwise.py     # hourly mirror of the Readwise Books finished shelf
  seed_from_csv.py     # one-time bootstrap from official CSV exports (historical)
  _common.py           # schema validation, atomic write, slug helpers
.github/workflows/
  poll.yml             # cron hourly at :17, workflow_dispatch; commits to main
  dispatch.yml         # on push to data/**, fans out repository_dispatch
```

## Required secrets

Set in **Settings → Secrets and variables → Actions**:

| Secret | Used by | Notes |
| --- | --- | --- |
| `LETTERBOXD_USERNAME` | `poll.yml` | e.g., `stultus` |
| `READWISE_TOKEN` | `poll.yml` | personal access token from https://readwise.io/access_token |
| `SITE_DEPLOY_PAT` | `dispatch.yml` | fine-grained PAT with `contents: write` on `stultus/stultus.in` and `stultus/notes-hugo` (needed to send `repository_dispatch`) |

The default `GITHUB_TOKEN` is sufficient for the poll job's commit step (`permissions: contents: write` is set in the workflow).

## How it runs

- `poll.yml` runs hourly (`17 * * * *`) and on manual dispatch. GitHub runs scheduled
  workflows on a best-effort basis, so in practice gaps of a few hours happen. Both poll steps are `continue-on-error`, so one broken source can't
  block the other's commit; a final step fails the job if either failed. It runs both
  poll scripts, and commits `data/` only if `git diff --quiet -- data/` returns non-zero. The
  commit message is `chore: feeds update`. `poll.yml` only triggers on schedule and manual
  dispatch (not push), so there is no self-trigger risk; a clean message also lets `dispatch.yml`
  fire on the bot commit.
- `dispatch.yml` triggers on pushes to `main` that touch `data/**`. It fans out
  `repository_dispatch` events with type `feeds-updated` to the two consumer repos in parallel
  (matrix strategy, `fail-fast: false`).

## Books source: Readwise

`poll_readwise.py` mirrors the **finished** shelf of Readwise Books
([API docs](https://readwise.io/books_api), marked experimental by Readwise) into `books.json`.
Readwise decides which books are listed, their `read_date` (`finished_reading_at`) and `rating`
(0.25–5 in quarter steps; whole numbers are stored as ints). Every run fetches the whole shelf
(one request for up to 1000 books), because Readwise doesn't bump `updated` when a rating
changes.

Entries are linked to Readwise by `readwise_id`. Once an entry exists, its title, author, slug,
cover and link are kept, so hand-picked covers and links survive syncs and book page URLs on
notes-hugo (built from `slug`) stay stable. A missing cover or ISBN is filled from Readwise's
catalogue match. A book that leaves the finished shelf leaves `books.json`. The run refuses to
write if the shelf comes back more than 20% smaller than `books.json`.

History: until November 2026 the books came from Goodreads (RSS, then public widgets after
Goodreads retired the RSS feeds). Readwise's library was imported from Goodreads, and the
one-time switch matched every existing entry to its Readwise book, keeping the Goodreads ids and
`goodreads_url` links for those entries. New books get `rw-<readwise id>` ids.

### Books not in Readwise's catalogue

Add them with the `add-book` skill (`.claude/skills/add-book/SKILL.md`; Codex finds it via
`AGENTS.md`). It adds the book to Readwise through the API, runs the poller, then sets the cover
(stored in `data/covers/`) and link on the new entry.

## Schema

### `movies.json`

```jsonc
{
  "id": "https://letterboxd.com/<user>/film/<slug>/",  // dedup key (Letterboxd URI)
  "title": "string",
  "year": 2023,
  "watched_date": "YYYY-MM-DD",
  "rating": 4.5,                  // float in 0.5..5.0, or null
  "rewatch": false,
  "review_html": "string|null",
  "tmdb_id": "string|null",
  "letterboxd_url": "string",
  "slug": "kebab-case-title-year"
}
```

### `books.json`

```jsonc
{
  "id": "<goodreads book id, or rw-<readwise id> for books added after the switch>",
  "readwise_id": 6503779,         // Readwise Books id, or null
  "title": "string",
  "author": "string",
  "read_date": "YYYY-MM-DD|null",
  "added_date": "YYYY-MM-DD",
  "rating": 4,                    // 0.25..5 in quarter steps (int when whole), or null
  "review_html": "string|null",
  "isbn": "string|null",
  "cover_url": "string|null",
  "goodreads_url": "string|null", // book link: Goodreads, a store or publisher page (name kept for compatibility)
  "slug": "kebab-case-title-author-lastname"
}
```

Validation lives in `scripts/_common.py:validate`. Unknown fields and missing fields both fail
the run, so schema drift is caught immediately.

## Bootstrapping historical data

RSS only returns the most recent ~50 entries. To seed `data/*.json` with full history:

**Letterboxd** — recommended path is to scrape your public diary, because the diary HTML carries
`data-viewing-id` which matches the RSS GUID exactly (`letterboxd-watch-NNNN`). That means seed +
RSS share an id space and dedupe cleanly forever after.

```bash
LETTERBOXD_USERNAME=stultus uv run scripts/seed_letterboxd_scrape.py
```

Defaults to a 1.5s delay between pages and a real-browser User-Agent. ~9 pages × 50 rows ≈ 450
movies in ~15 seconds. If your IP is bot-flagged (rare for residential networks), pass
`--user-agent` to override. Run from your home machine, not a cloud / VPN IP.

Fields populated by the scrape: `id`, `title`, `year`, `watched_date`, `rating`, `rewatch`,
`slug`, `letterboxd_url`. The diary listing does not expose `review_html` or `tmdb_id`; the
hourly RSS poll fills both for the most recent ~50 watches as they appear in the feed.

Alternatively, the CSV path (`scripts/seed_from_csv.py letterboxd path/to/letterboxd-export.zip`)
also works but uses a different id format (`Letterboxd URI` short link), so the next RSS poll
will produce ~50 duplicates of your most recent watches that you'd have to clean up manually. The
scrape avoids that.

**Goodreads**: My Books → Import and export → Export Library → download CSV.

```bash
uv run scripts/seed_from_csv.py goodreads ~/Downloads/goodreads_library_export.csv
```

Filters to rows where `Exclusive Shelf == "read"`. This was the original bootstrap; the seeder
predates `readwise_id` and is kept for history only.

The seeder **overwrites** `data/movies.json` / `data/books.json`. Run it once at repo bootstrap
and never again — the pollers handle ongoing updates.

## Local testing

```bash
uv sync
export LETTERBOXD_USERNAME=stultus
export READWISE_TOKEN=...

uv run scripts/poll_letterboxd.py --dry-run
uv run scripts/poll_readwise.py --dry-run
```

`--dry-run` prints a unified diff of what would change and exits 0 without writing. Both pollers
are idempotent: running twice produces no diff.

## Design choices (worth knowing)

- **Direct commits to `main`, not PRs.** This is a personal repo with a deterministic writer; a
  PR per poll would be noise.
- **No retry/backoff.** A failed cron run is fine — the next one picks up the same feed state.
- **Atomic writes.** All `data/*.json` writes go through `tempfile + os.replace` in the same
  directory, so a crashed run never leaves a half-written file.
- **Canonical serialization.** JSON is written with `indent=2`, `sort_keys=True`,
  `ensure_ascii=False`, and a trailing newline. The "no real diff → no commit" check compares
  byte-for-byte against the same format, so reordering or whitespace churn never produces a commit.
- **Schema is closed.** `validate()` rejects both missing and unexpected fields. Adding a field
  requires updating `_common.py:MOVIE_FIELDS`/`BOOK_FIELDS`.
- **Author lastname for book slug.** `book_slug("The Idiot", "Fyodor Dostoevsky")` →
  `the-idiot-dostoevsky`. Single-name authors degrade gracefully.
- **`read_date` may be null** for books Readwise has no finish date for; sort puts those last but the
  `added_date` gives a stable secondary order.
