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

**Just want it on your phone?** See [DEPLOY.md](DEPLOY.md) — a step-by-step
guide that needs nothing but a browser.

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

A TV Time GDPR export (`gdprdata.zip`) is about fifty CSVs dumped straight out
of their databases. Only eight of them have anything to do with viewing
history; the rest are access tokens, IP logs, device records and Facebook
likes. Several of those have columns generic enough — `name`, `region_name` —
that a naive importer will happily follow a city or a Facebook page as if it
were a TV show. So the importer recognises the export and reads only the files
that matter:

| File | What is taken from it |
| --- | --- |
| `tracking-prod-records-v2.csv` | The full watch history (TVDB id, season, episode, date) and current follow/archive state |
| `followed_tv_show.csv`, `user_tv_show_data.csv` | Follow and archive state for older accounts |
| `tracking-prod-records.csv`, `seen_episode_source.csv`, `seen_episode_latest.csv`, `rewatched_episode.csv`, `watched_on_episode.csv` | Older per-feature history tables |

Everything else is listed in the report as ignored. Shows are matched to TVmaze
through the TVDB id TV Time stores, falling back to a title search only when
that fails. Older tables record a show by name and newer ones by id, so the two
are folded together before anything is looked up.

Your follow state is carried across rather than flattened: shows you had
archived come in archived, and shows you had unfollowed stay out of your
library while keeping their watch history in your stats.

In the app: **More → Import from TV Time**, choose the zip, press **Preview**.
Nothing is written yet — you get a report of what was found, which shows could
not be identified, and which were matched by title alone. If it looks right,
press **Import for real**.

From the command line:

```bash
python -m app.cli import ~/Downloads/gdprdata.zip           # preview
python -m app.cli import ~/Downloads/gdprdata.zip --commit  # apply
```

An import of a few hundred shows takes several minutes, because TVmaze is rate
limited and every show needs its episode list. It runs in the background and
the page reports progress, so you can close the tab and come back. Importing is
idempotent — running it twice will not double-count anything.

Two things will show up in the report and are worth knowing about:

- **Unnumbered specials.** TV Time files specials as season 0 or episode 0,
  which is a placeholder rather than a number, so those rows have nothing to
  match against and are counted separately from real misses.
- **Shows TVmaze does not have under that TVDB id** — usually TV movies. These
  are named in the report rather than dropped silently, along with anything
  matched by title alone, which is where a wrong match would hide.

If you have some other CSV instead, the importer falls back to sniffing column
headers: anything with a show, a season, an episode and a date will import.

## How "next up" works

The next episode for a show is the **first unwatched regular episode in air
order** — the first gap in your run, which is what TV Time did. Specials are
kept out of progress and up-next but are still listed and tickable on the show
page.

If you watched a show out of order, that first gap can be a long way behind
where you actually got to, so the card says how many earlier episodes are
unmarked. The `⟵` button next to any episode marks it and everything before it
as watched, which clears the backlog in one tap.

A show lands in one of six groups:

| Group | Meaning |
| --- | --- |
| Priority watch | You pinned it and it has an episode waiting |
| Ready to watch | You have started it and the next episode has aired |
| Coming up | You are current; the next episode has a date |
| Not started yet | You follow it but have not watched an episode |
| Waiting for more | You are current; nothing is scheduled yet |
| Finished | The show has ended and you have seen it all |

"Ready to watch" is ordered by when you last watched each show, so whatever you
were in the middle of is at the top. Shows you follow but never started are
split out rather than mixed in — with a few hundred followed shows they would
otherwise bury everything you are actually watching.

## Favorites and priority watch

Two independent flags, both toggled from a show's page:

- **★ Favorite** is a permanent label for shows you love. It puts a star on the
  show wherever it appears and gives you a Favorites filter on the Shows tab.
  It does not change any ordering — a favorite you are caught up on stays where
  it belongs.
- **● Priority watch** is "get to this next". A priority show with an aired
  episode waiting is pinned to its own section at the top of Up Next, above
  everything else. Pin a few things you want to get through and they stay in
  front of you until you do.

The distinction matters when your library is large: favorites answer "what do I
love", priority answers "what am I watching this week". A priority show you are
caught up on drops back to its normal group rather than sitting at the top with
nothing to click, so the section only ever holds things you can act on.

The Shows tab filters by Following, Favorites, Priority watch, Never started
or Archived. Favorites also import from TV Time, which stored an
`is_favorited` flag.

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

Your watch history is the only thing here that cannot be fetched again — show
and episode data comes back from TVmaze any time, but "I watched this" does
not, and TV Time is gone. So the app backs itself up rather than relying on
you remembering.

A dated JSON snapshot is written to `data/backups/` every 24 hours, keeping the
last 14. A snapshot whose contents match the previous one is skipped, so a
quiet fortnight cannot push your real history out of the retention window.
Writes go to a temporary file and are renamed into place, so an interrupted
backup cannot leave a corrupt file that looks valid.

**More → Backups** lists them, downloads any one, and has a "Back up now"
button. Download one to your phone or computer occasionally: a backup on the
same disk as the database only protects you from mistakes, not from losing the
disk.

Restoring: `POST /api/restore` with a snapshot rebuilds the library from
scratch, re-fetching each show from TVmaze. Or just copy `data/tv.db` — that is
the whole library in one file.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TV_BACKUP_ENABLED` | `true` | Turn automatic backups off |
| `TV_BACKUP_DIR` | `$TV_DATA_DIR/backups` | Where snapshots go |
| `TV_BACKUP_INTERVAL_HOURS` | `24` | How often to snapshot |
| `TV_BACKUP_KEEP` | `14` | How many to keep |

## Tidying a big library

An import from years of TV Time brings a long tail of shows you followed once
and never watched. On the **Shows** tab, set the filter to **Never started**,
tap **Select**, then **Select all**, then **Archive** — the whole tail is out
of your way in one go. The same selection mode does favourites, priority,
unarchiving and removing.

Removing a show never touches your watch history; it stays in your stats and
comes back if you add the show again.

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
