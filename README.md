# hrishi-feeds

Personal data ingestion. Polls Letterboxd and Goodreads RSS feeds on a schedule, normalizes them
to versioned JSON in `data/`, and triggers downstream Hugo rebuilds via `repository_dispatch`.

The repo is single-purpose: scheduled poll → diff data files → commit on real change → notify
consumers (`stultus/stultus.in`, `stultus/notes`).

## Layout

```
data/
  movies.json          # array, sorted by watched_date desc
  books.json           # array, sorted by read_date desc (nulls last)
scripts/
  poll_letterboxd.py   # daily poll
  poll_goodreads.py    # daily poll
  seed_from_csv.py     # one-time bootstrap from official CSV exports
  _common.py           # schema validation, atomic write, slug helpers
.github/workflows/
  poll.yml             # cron 04:00 UTC + workflow_dispatch; commits to main
  dispatch.yml         # on push to data/**, fans out repository_dispatch
```

## Required secrets

Set in **Settings → Secrets and variables → Actions**:

| Secret | Used by | Notes |
| --- | --- | --- |
| `LETTERBOXD_USERNAME` | `poll.yml` | e.g., `stultus` |
| `GOODREADS_USER_ID` | `poll.yml` | numeric, from your Goodreads profile URL |
| `GOODREADS_RSS_KEY` | `poll.yml` | the `key=` query param from the RSS link on your "read" shelf |
| `SITE_DEPLOY_PAT` | `dispatch.yml` | fine-grained PAT with `contents: write` on `stultus/stultus.in` and `stultus/notes` (needed to send `repository_dispatch`) |

The default `GITHUB_TOKEN` is sufficient for the poll job's commit step (`permissions: contents: write` is set in the workflow).

## How it runs

- `poll.yml` runs at `0 4 * * *` UTC and on manual dispatch. It checks out the repo, runs both
  poll scripts, and commits `data/` only if `git diff --quiet -- data/` returns non-zero. The
  commit message is `chore: feeds update [skip ci]` so it does not retrigger itself.
- `dispatch.yml` triggers on pushes to `main` that touch `data/**`. It fans out
  `repository_dispatch` events with type `feeds-updated` to the two consumer repos in parallel
  (matrix strategy, `fail-fast: false`).

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
  "id": "<goodreads numeric book id>",
  "title": "string",
  "author": "string",
  "read_date": "YYYY-MM-DD|null",
  "added_date": "YYYY-MM-DD",
  "rating": 4,                    // int in 1..5, or null
  "review_html": "string|null",
  "isbn": "string|null",
  "cover_url": "string|null",
  "goodreads_url": "string",
  "slug": "kebab-case-title-author-lastname"
}
```

Validation lives in `scripts/_common.py:validate`. Unknown fields and missing fields both fail
the run, so schema drift is caught immediately.

## Bootstrapping historical data

RSS only returns the most recent ~50 entries. To seed `data/*.json` with full history:

**Letterboxd**: Settings → Import & Export → "Export your data" → unzip is **not** required, the
seeder reads the ZIP directly.

```bash
uv run scripts/seed_from_csv.py letterboxd ~/Downloads/letterboxd-export.zip
```

This reads `diary.csv` (Watched Date, Rating, Rewatch, Letterboxd URI) and enriches with
`reviews.csv` if present. The Letterboxd URI is used as the dedup `id`, so subsequent RSS polls
upsert cleanly.

**Goodreads**: My Books → Import and export → Export Library → download CSV.

```bash
uv run scripts/seed_from_csv.py goodreads ~/Downloads/goodreads_library_export.csv
```

Filters to rows where `Exclusive Shelf == "read"`. Cover URLs are not in the CSV, so they stay
`null` until the next RSS poll fills them in.

The seeder **overwrites** `data/movies.json` / `data/books.json`. Run it once at repo bootstrap
and never again — RSS pollers handle ongoing updates.

## Local testing

```bash
uv sync
export LETTERBOXD_USERNAME=stultus
export GOODREADS_USER_ID=12345678
export GOODREADS_RSS_KEY=abc123

uv run scripts/poll_letterboxd.py --dry-run
uv run scripts/poll_goodreads.py --dry-run
```

`--dry-run` prints a unified diff of what would change and exits 0 without writing. Both pollers
are idempotent: running twice produces no diff.

## Design choices (worth knowing)

- **Direct commits to `main`, not PRs.** This is a personal repo with a deterministic writer; a
  PR per poll would be noise.
- **No retry/backoff.** A failed cron run is fine — the next one picks up the same RSS state.
- **Atomic writes.** All `data/*.json` writes go through `tempfile + os.replace` in the same
  directory, so a crashed run never leaves a half-written file.
- **Canonical serialization.** JSON is written with `indent=2`, `sort_keys=True`,
  `ensure_ascii=False`, and a trailing newline. The "no real diff → no commit" check compares
  byte-for-byte against the same format, so reordering or whitespace churn never produces a commit.
- **Schema is closed.** `validate()` rejects both missing and unexpected fields. Adding a field
  requires updating `_common.py:MOVIE_FIELDS`/`BOOK_FIELDS`.
- **Author lastname for book slug.** `book_slug("The Idiot", "Fyodor Dostoevsky")` →
  `the-idiot-dostoevsky`. Single-name authors degrade gracefully.
- **Goodreads `read_date` may be null** for shelf-only adds; sort puts those last but the
  `added_date` gives a stable secondary order.
