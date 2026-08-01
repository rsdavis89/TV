# TV Tracker

A self-hosted replacement for TV Time, built around the three things that
actually mattered: it remembers what you have watched, it tells you what is
next in every show, and it tells you when something new has aired.

No social features, no accounts, no ads. Your watch history is a SQLite file
you own.

- **Up Next** — every show you follow, grouped into what you can watch now,
  what is coming, and what you have finished. One tap marks an episode watched
  and rolls the show forward.
- **New** — episodes that aired since your last visit, with a badge on the tab.
- **Calendar** — the next five weeks of airings for the shows you follow.
- **Shows** — your library with progress bars, per-episode ticks, "watch all
  through here", season toggles, and archiving for shows you have set aside.
- **Import** — bring your TV Time export in, with a preview before anything is
  written.
- Installs to your phone's home screen as a PWA and works offline for browsing.

Show and episode data comes from [TVmaze](https://www.tvmaze.com/api), which is
free and needs no API key.

## Quick start

```bash
git clone <this repo> tv-tracker && cd tv-tracker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

Open <http://localhost:8484>. On your phone, open the same address on your
network and use "Add to Home Screen" to install it.

With Docker instead:

```bash
docker compose up -d
```

## Importing your TV Time export

TV Time's export is a zip of CSVs, and its exact shape has changed over the
years. Rather than assume one layout, the importer reads the header of every
CSV in the archive and works out which columns hold the show, season, episode
and watch date. Shows are matched to TVmaze by their TVDB id where the export
has one, and by title otherwise.

In the app: **More → Import from TV Time**, choose the zip, press **Preview**.
Nothing is written yet — you get a report of what was found, which shows could
not be identified, and which were matched by title alone (worth a glance, since
titles are ambiguous). If it looks right, press **Import for real**.

From the command line:

```bash
python -m app.cli import ~/Downloads/tv-time-export.zip           # preview
python -m app.cli import ~/Downloads/tv-time-export.zip --commit  # apply
```

Importing is idempotent — running it twice will not double-count anything, so
it is safe to re-run after fixing up a CSV by hand.

If your export turns out to use a layout the sniffer does not recognise, any
CSV with a show column, a season column, an episode column and a date column
will import, so a quick spreadsheet reshape is always a fallback. The preview
report names the columns it picked for each file, which tells you what it saw.

## How "next up" works

The next episode for a show is the **first unwatched regular episode in air
order** — the first gap in your run, which is what TV Time did. Specials are
kept out of progress and up-next but are still listed and tickable on the show
page.

If you have gaps you do not care about, the `⟵` button next to any episode
marks it and everything before it as watched.

A show lands in one of four groups:

| Group | Meaning |
| --- | --- |
| Ready to watch | The next episode has aired |
| Coming up | You are current; the next episode has a date |
| Waiting for more | You are current; nothing is scheduled yet |
| Finished | The show has ended and you have seen it all |

## New episode alerts

A background job asks TVmaze every few hours which of your shows have changed,
and re-syncs only those. New episodes show up on the **New** tab with a count
badge on the tab bar; the badge clears when you open it. **More → Check now**
runs the check immediately.

There are no push notifications or emails by design — the app tells you when
you open it. (If you later want push, `refresh.refresh_all()` returns the
episodes it found, which is the natural place to hook one in.)

## Configuration

All optional; see `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TV_DATA_DIR` | `./data` | Where `tv.db` lives |
| `TV_DB_PATH` | `$TV_DATA_DIR/tv.db` | Override the database path outright |
| `TV_PASSWORD` | *(empty)* | Set to require a password. Empty means no login |
| `TV_SECRET_KEY` | generated | Session signing key; generated and stored if unset |
| `TV_SESSION_DAYS` | `90` | How long a login lasts |
| `TV_REFRESH_INTERVAL_HOURS` | `6` | How often to look for new episodes |
| `TV_REFRESH_ON_START` | `true` | Run a check at startup |
| `TV_STALE_SHOW_HOURS` | `168` | Re-sync a show at least this often regardless |
| `TV_HOST` / `TV_PORT` | `0.0.0.0` / `8484` | Where to listen |

`TV_PASSWORD` is a single shared password, not a user system — enough to keep a
tracker off the open internet, and not a substitute for putting it behind a
reverse proxy with TLS if you expose it.

## Command line

```bash
python -m app.cli next --upcoming 14   # what to watch, plus two weeks ahead
python -m app.cli add "slow horses"    # follow the best match
python -m app.cli refresh --force      # re-sync every followed show
python -m app.cli import <path>        # see above
```

## Backups

**More → Download backup** writes a JSON file with every show you follow and
every episode you have watched. `POST /api/restore` reads it back. Or just copy
`data/tv.db` — that is the whole library.

## Layout

```
app/
  main.py      FastAPI app, static hosting, refresh loop
  api.py       HTTP endpoints
  library.py   follows, watches, up-next, progress, stats
  importer.py  TV Time / generic CSV import
  tvmaze.py    TVmaze client with rate limiting
  refresh.py   background new-episode checks
  db.py        SQLite schema and connections
  cli.py       command line entry points
web/           the PWA: one HTML file, one JS file, one stylesheet, no build step
tools/         icon generator
tests/         pytest suite
```

The front end is deliberately dependency-free — no npm, no bundler, no build
step. Editing `web/app.js` and reloading is the whole workflow.

## Development

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest              # unit tests, no network needed
python tools/make_icons.py    # regenerate PWA icons
```

Interactive API docs run at `/api/docs` while the server is up.

## A note on data

TVmaze episode numbering is occasionally different from TVDB's, which is what
TV Time used. Where a watched episode from your export has no counterpart on
TVmaze, the import report lists it rather than dropping it quietly, so you can
tick those few by hand.
