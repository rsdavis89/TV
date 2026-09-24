# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A self-hosted TV Time replacement: FastAPI + SQLite behind a vanilla-JS PWA, with
show data from TVmaze (free, no API key). See README.md for the feature tour and
DEPLOY.md for the Railway walkthrough.

## Commands

```bash
# Setup. Test deps are deliberately not in requirements.txt - install them too.
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt pytest pytest-asyncio

./.venv/bin/python -m pytest -q                              # whole suite
./.venv/bin/python -m pytest tests/test_library.py -q         # one file
./.venv/bin/python -m pytest -q -k "significant_special"      # one test

TV_DATA_DIR=/tmp/tv ./.venv/bin/python -m app.main            # serve on :8484
```

`pytest.ini` sets `asyncio_mode = auto`, so async tests need no decorator. There is
no linter, formatter or type checker configured, and the front end has no build
step - `web/app.js` is served as written.

## Deployment

Railway auto-deploys the repository's default branch. **A push is a production
deploy**; there is no staging. `web/sw.js` fetches the app shell network-first, so
a new `app.js` reaches clients on the next open rather than a version later.

## Architecture

TVmaze -> `app/tvmaze.py` (rate-limited async client) -> `app/library.py` (sync and
queries) -> `app/api.py` -> `web/app.js`.

**Two independent pipelines** pull from TVmaze and rarely share code:

- **Episodes** (`episode` table), for shows you follow. Written *only* by
  `library.sync_show()`, driven by `refresh.refresh_all()`.
- **Premieres** (`premiere` table), for discovering shows you do not follow.
  Written *only* by `premieres.sweep()`, which walks the TVmaze schedule.

`main.py`'s lifespan starts three background schedulers as tasks - refresh, backup,
premieres. They share one process-wide rate limiter (`tvmaze._limiter`, 18 requests
per 10s), so they contend with each other. `REFRESH_ON_START` defaults to true, so
every deploy re-syncs the library at boot.

`get_show_with_episodes()` deliberately makes **two** requests. `?specials=1` is
honoured by `/shows/{id}/episodes` and *silently ignored* by `embed[]=episodes`.
Collapsing it back to one call makes every special disappear without an error.

**Staleness.** `refresh_all()` skips any show synced within `STALE_SHOW_HOURS`
(7 days) unless TVmaze's updates feed flags it. A fix that changes *what a sync
collects* therefore does not reach data already on record. Bump `SYNC_GENERATION`
(`app/refresh.py`) to force one full pass over every followed show; it is recorded
only after the pass completes, so an interrupted pass retries whole. A show that
fails to sync never stops the pass: its `synced_at` is cleared so the next pass
retries it, and only it. `premieres.py`
does the same with `SWEEP_GENERATION`. To check a sync change against one show
immediately, use per-show Re-sync (`POST /api/shows/{id}/refresh`) - it calls
`sync_show` directly and ignores staleness.

**Specials.** `library.is_special()` decides what is main-line. A
`significant_special` stays main-line *even with no episode number*, because TVmaze
omits numbers on between-season one-offs; season 0 is always special. `MAIN =
"e.is_special = 0"` gates progress, up-next and the New tab. Unnumbered episodes
sort first within their season - `next_episode`, `_order_key` and `show_detail` all
agree on that, and they must keep agreeing.

**Auth** is one middleware in `main.py` gating everything under `/api` except
`OPEN_PATHS`. Static files are open.

**Imports** (`app/importer.py`, the largest module) parse a TV Time GDPR export,
whose ~50 CSVs include several that would fool a naive column sniffer. Imports run
as background jobs (`app/jobs.py`) that the page polls, since a full library takes
minutes under the rate limit. `app/cli.py` offers the same operations offline.

## Time handling

Every bug found here so far has been a timezone bug. The rules:

- **`airstamp` is normalised to UTC** (`library.normalize_airstamp`) so it compares
  and sorts as a plain string. All ordering and windowing uses it.
- **`airtime` is TVmaze's network-local time string, and empty means TVmaze has no
  time on record.** TVmaze stamps an unknown time as 12:00 UTC, which renders as a
  convincing 8:00 AM in New York. NULL means the row predates the column and keeps
  its old behaviour. `library.time_known()` is the single rule for this; the API
  ships `time_known` to the front end, which then prints the date alone.
- **The calendar groups by local day** (`localDay()` in `web/app.js`), never by
  slicing the UTC airstamp. The time shown beside a row is local, so slicing UTC
  files a 9pm Sunday airing under Monday - anything after 8pm Eastern is already
  past midnight UTC.

## Conventions that will bite you

1. **Bump `APP_VERSION` in `web/app.js` whenever that file changes, and never
   reuse a number.** The server reads the constant out of the file it would serve
   and compares it with what the browser reports; that is the stale-cache check.
   Reverting and re-landing under the same number leaves two different builds
   claiming it and blinds the check - the symptom is a client running old code
   that believes it is current.
2. **New `ALTER TABLE` migrations follow the v7 pattern in `app/db.py`**: a
   callable in `SCHEMA` that adds only the columns that are missing. `SCHEMA`
   entries may be SQL strings or callables taking a connection. `ALTER TABLE` has
   no `IF NOT EXISTS`, so a multi-statement script that half-applies leaves the
   version unadvanced, re-runs from the top next boot and aborts on "duplicate
   column name" - inside lifespan, before the port is bound. That wedges every
   subsequent start, not just the one that went wrong.
3. **Tests stub `tvmaze.get_show_with_episodes` wholesale and cannot see which URL
   was requested** - which is how the specials bug survived. When changing request
   shape, stub `tvmaze._get` and assert on the paths and params instead; there are
   examples in `tests/test_library.py`.
