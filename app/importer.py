"""Import a TV Time data export (or any comparable CSV) into the library.

A real TV Time export is a zip of ~50 CSVs dumped straight out of their
databases. Most of them have nothing to do with viewing history — access
tokens, IP logs, Facebook likes — and several of those have columns generic
enough ("name", "region_name") to fool a naive column sniffer into treating a
city or a Facebook page as a TV show. So this module works in two modes:

* If the archive looks like a TV Time export, it reads the specific files that
  hold history and follow state, and ignores everything else.
* Otherwise it falls back to sniffing column headers, which handles hand-made
  CSVs and whatever shape a future export takes.

Either way, nothing is written until the caller asks for it, and anything that
could not be matched ends up in the report rather than being dropped quietly.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import library, tvmaze
from .db import connect, tx, utcnow

Progress = Callable[[str, int, int], None]

# --------------------------------------------------------------------------
# generic column sniffing
# --------------------------------------------------------------------------

# Candidates are listed most specific first. Headers are normalised to
# lowercase with runs of non-alphanumerics collapsed to a single underscore.
SHOW_ID_COLUMNS = [
    "tvdb_id",
    "thetvdb_id",
    "thetvdb",
    "series_tvdb_id",
    "tv_show_id",
    "show_id",
    "series_id",
    "tvdb",
    "s_id",
]
SHOW_NAME_COLUMNS = [
    "tv_show_name",
    "show_name",
    "series_name",
    "series_title",
    "show_title",
    "tv_show",
    "show",
    "series",
    "name",
    "title",
]
SEASON_COLUMNS = ["season_number", "season_no", "season_num", "season"]
EPISODE_COLUMNS = [
    "episode_number",
    "episode_no",
    "episode_num",
    "number",
    "episode",
    "ep_number",
]
WATCHED_COLUMNS = [
    "first_watched",
    "first_watched_at",
    "watched_at",
    "watched_date",
    "date_watched",
    "seen_at",
    "watch_date",
    "created_at",
    "updated_at",
    "timestamp",
    "date",
]
FOLLOWED_COLUMNS = ["is_followed", "followed", "active", "following"]
ARCHIVED_COLUMNS = ["is_archived", "archived"]

# Names too generic to fuzzy-match on. "name" must not be allowed to match
# "region_name", and "number" must not match "version_number".
GENERIC_COLUMNS = {
    "name",
    "title",
    "show",
    "series",
    "number",
    "episode",
    "season",
    "date",
    "timestamp",
    "active",
    "followed",
    "archived",
}

YEAR_SUFFIX = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")
NON_ALNUM = re.compile(r"[^a-z0-9]+")
DIGITS = re.compile(r"\d+")
TRUE_VALUES = {"1", "true", "yes", "y", "t"}
FALSE_VALUES = {"0", "false", "no", "n", "f"}


def normalize_header(name: str) -> str:
    return NON_ALNUM.sub("_", (name or "").strip().lower()).strip("_")


def normalize_title(name: str) -> str:
    lowered = YEAR_SUFFIX.sub("", (name or "").strip().lower())
    return NON_ALNUM.sub(" ", lowered).strip()


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = DIGITS.search(str(value))
    return int(match.group()) if match else None


def parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def parse_timestamp(value: Any) -> str | None:
    """Accept ISO strings, common SQL datetimes and epoch numbers."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "-"}:
        return None

    if text.isdigit():
        number = int(text)
        if number > 10**12:  # milliseconds
            number //= 1000
        if 10**8 < number < 10**11:
            return (
                datetime.fromtimestamp(number, tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
            )

    cleaned = text.replace("Z", "+00:00").replace("/", "-")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y %H:%M:%S"):
            try:
                parsed = datetime.strptime(cleaned.split(".")[0], pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def pick_column(headers: Sequence[str], candidates: Sequence[str]) -> str | None:
    """Exact match first, then a suffix/prefix match for specific names only."""
    lookup = set(headers)
    for candidate in candidates:
        if candidate in lookup:
            return candidate
    for candidate in candidates:
        if candidate in GENERIC_COLUMNS:
            continue
        for header in headers:
            if header.endswith(f"_{candidate}") or header.startswith(f"{candidate}_"):
                return header
    return None


@dataclass
class ColumnMap:
    show_id: str | None = None
    show_name: str | None = None
    season: str | None = None
    episode: str | None = None
    watched: str | None = None
    followed: str | None = None
    archived: str | None = None

    @property
    def has_show(self) -> bool:
        return bool(self.show_id or self.show_name)

    @property
    def has_episode(self) -> bool:
        return bool(self.season and self.episode)

    def describe(self) -> dict[str, str | None]:
        return {
            "show_id": self.show_id,
            "show_name": self.show_name,
            "season": self.season,
            "episode": self.episode,
            "watched_at": self.watched,
            "followed": self.followed,
            "archived": self.archived,
        }


def detect_columns(headers: Sequence[str]) -> ColumnMap:
    normalized = [normalize_header(header) for header in headers]
    mapping = ColumnMap(
        show_id=pick_column(normalized, SHOW_ID_COLUMNS),
        show_name=pick_column(normalized, SHOW_NAME_COLUMNS),
        season=pick_column(normalized, SEASON_COLUMNS),
        episode=pick_column(normalized, EPISODE_COLUMNS),
        watched=pick_column(normalized, WATCHED_COLUMNS),
        followed=pick_column(normalized, FOLLOWED_COLUMNS),
        archived=pick_column(normalized, ARCHIVED_COLUMNS),
    )
    # "episode_id" is an identifier, never an episode number.
    if mapping.episode and mapping.episode.endswith("_id"):
        mapping.episode = None
    if mapping.show_name and mapping.show_name == mapping.show_id:
        mapping.show_name = None
    return mapping


# --------------------------------------------------------------------------
# what we collect
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ShowKey:
    tvdb_id: int | None
    name: str | None

    def identity(self) -> tuple[str, Any]:
        """Two rows describe the same show if their TVDB id, or title, agree."""
        if self.tvdb_id:
            return ("tvdb", self.tvdb_id)
        return ("name", normalize_title(self.name or ""))

    def label(self) -> str:
        if self.name and self.tvdb_id:
            return f"{self.name} (tvdb {self.tvdb_id})"
        if self.name:
            return self.name
        return f"tvdb {self.tvdb_id}"


@dataclass
class ShowRecord:
    key: ShowKey
    followed: bool | None = None
    archived: bool | None = None
    favorite: bool | None = None

    def learn(self, key: ShowKey, **flags: bool | None) -> None:
        """Fill in anything we do not know yet. Earlier sources win."""
        if self.key.tvdb_id is None and key.tvdb_id is not None:
            self.key = ShowKey(key.tvdb_id, self.key.name or key.name)
        if not self.key.name and key.name:
            self.key = ShowKey(self.key.tvdb_id, key.name)
        for field_name, value in flags.items():
            if value is not None and getattr(self, field_name) is None:
                setattr(self, field_name, value)


@dataclass
class WatchRecord:
    identity: tuple[str, Any]
    season: int
    number: int
    watched_at: str | None


@dataclass
class ParsedSource:
    watches: list[WatchRecord] = field(default_factory=list)
    shows: dict[tuple[str, Any], ShowRecord] = field(default_factory=dict)
    files: list[dict] = field(default_factory=list)
    format: str = "generic CSV"

    def record(self, key: ShowKey, **flags: bool | None) -> ShowRecord:
        identity = key.identity()
        existing = self.shows.get(identity)
        if existing is None:
            existing = ShowRecord(key=key)
            self.shows[identity] = existing
        existing.learn(key, **flags)
        return existing


# --------------------------------------------------------------------------
# reading files out of the export
# --------------------------------------------------------------------------


def iter_tables(source: Path) -> Iterable[tuple[str, bytes]]:
    """Yield (name, bytes) for every CSV/JSON in a zip, directory or file."""
    if source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.suffix.lower() in {".csv", ".json"} and path.is_file():
                yield str(path.relative_to(source)), path.read_bytes()
        return
    if source.suffix.lower() == ".zip" or zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if Path(info.filename).suffix.lower() in {".csv", ".json"}:
                    yield info.filename, archive.read(info)
        return
    yield source.name, source.read_bytes()


def read_rows(name: str, blob: bytes) -> list[dict]:
    text = blob.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return []
    if name.lower().endswith(".json"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, list):
                    payload = value
                    break
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    sample = text[:8192]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = "excel"
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [row for row in reader if any((value or "").strip() for value in row.values())]


def load_tables(source: Path) -> dict[str, list[dict]]:
    """Read the whole export once, keyed by base filename."""
    tables: dict[str, list[dict]] = {}
    for name, blob in iter_tables(source):
        rows = read_rows(name, blob)
        normalized = [
            {normalize_header(key): value for key, value in row.items()} for row in rows
        ]
        tables[Path(name).name.lower()] = normalized
    return tables


# --------------------------------------------------------------------------
# the TV Time export, read on its own terms
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchFile:
    filename: str
    season: str
    episode: str
    watched: str
    show_id: str | None = None
    show_name: str | None = None
    key_column: str | None = None
    key_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FollowFile:
    filename: str
    followed: str
    show_id: str | None = None
    show_name: str | None = None
    archived: str | None = None
    favorite: str | None = None
    key_column: str | None = None
    key_prefixes: tuple[str, ...] = ()


# tracking-prod-records-v2 is the modern history table and carries everything:
# the TVDB series id in `s_id`, season/episode numbers, and the watch time. The
# older per-feature tables are read too, since accounts that predate the
# migration may only have those.
TVTIME_WATCH_FILES = (
    WatchFile(
        filename="tracking-prod-records-v2.csv",
        show_id="s_id",
        show_name="series_name",
        season="season_number",
        episode="episode_number",
        watched="created_at",
        key_column="key",
        key_prefixes=("watch-episode", "rewatch-episode"),
    ),
    WatchFile(
        filename="tracking-prod-records.csv",
        show_id="series_id",
        show_name="series_name",
        season="season_number",
        episode="episode_number",
        watched="watch_date",
    ),
    WatchFile(
        filename="seen_episode_source.csv",
        show_name="tv_show_name",
        season="episode_season_number",
        episode="episode_number",
        watched="created_at",
    ),
    WatchFile(
        filename="seen_episode_latest.csv",
        show_name="tv_show_name",
        season="episode_season_number",
        episode="episode_number",
        watched="created_at",
    ),
    WatchFile(
        filename="rewatched_episode.csv",
        show_name="tv_show_name",
        season="episode_season_number",
        episode="episode_number",
        watched="created_at",
    ),
    WatchFile(
        filename="watched_on_episode.csv",
        show_name="tv_show_name",
        season="episode_season_number",
        episode="episode_number",
        watched="created_at",
    ),
)

# Ordered by how current the source is; the first file to state an opinion
# about a show wins.
TVTIME_FOLLOW_FILES = (
    FollowFile(
        filename="tracking-prod-records-v2.csv",
        show_id="s_id",
        show_name="series_name",
        followed="is_followed",
        archived="is_archived",
        key_column="key",
        key_prefixes=("user-series",),
    ),
    FollowFile(
        filename="followed_tv_show.csv",
        show_id="tv_show_id",
        show_name="tv_show_name",
        followed="active",
        archived="archived",
    ),
    FollowFile(
        filename="user_tv_show_data.csv",
        show_id="tv_show_id",
        show_name="tv_show_name",
        followed="is_followed",
        favorite="is_favorited",
    ),
)

TVTIME_MARKERS = {
    "tracking-prod-records-v2.csv",
    "followed_tv_show.csv",
    "seen_episode_source.csv",
    "user_tv_show_data.csv",
}


def looks_like_tvtime(tables: dict[str, list[dict]]) -> bool:
    return bool(TVTIME_MARKERS & set(tables))


def _key_matches(row: dict, column: str | None, prefixes: tuple[str, ...]) -> bool:
    if not column or not prefixes:
        return True
    value = (row.get(column) or "").strip()
    return any(value.startswith(prefix) for prefix in prefixes)


def _show_key(row: dict, id_column: str | None, name_column: str | None) -> ShowKey | None:
    tvdb_id = parse_int(row.get(id_column)) if id_column else None
    name = (row.get(name_column) or "").strip() if name_column else ""
    if not tvdb_id and not name:
        return None
    return ShowKey(tvdb_id=tvdb_id or None, name=name or None)


def parse_tvtime(tables: dict[str, list[dict]]) -> ParsedSource:
    parsed = ParsedSource(format="TV Time export")
    used: set[str] = set()

    for spec in TVTIME_FOLLOW_FILES:
        rows = tables.get(spec.filename)
        if not rows:
            continue
        used.add(spec.filename)
        counted = 0
        for row in rows:
            if not _key_matches(row, spec.key_column, spec.key_prefixes):
                continue
            key = _show_key(row, spec.show_id, spec.show_name)
            if key is None:
                continue
            parsed.record(
                key,
                followed=parse_bool(row.get(spec.followed)) if spec.followed else None,
                archived=parse_bool(row.get(spec.archived)) if spec.archived else None,
                favorite=parse_bool(row.get(spec.favorite)) if spec.favorite else None,
            )
            counted += 1
        parsed.files.append(
            {"file": spec.filename, "rows": len(rows), "used": f"follow state ({counted} shows)"}
        )

    for spec in TVTIME_WATCH_FILES:
        rows = tables.get(spec.filename)
        if not rows:
            continue
        counted = 0
        for row in rows:
            if not _key_matches(row, spec.key_column, spec.key_prefixes):
                continue
            key = _show_key(row, spec.show_id, spec.show_name)
            if key is None:
                continue
            season = parse_int(row.get(spec.season))
            number = parse_int(row.get(spec.episode))
            if season is None or number is None:
                continue
            record = parsed.record(key)
            parsed.watches.append(
                WatchRecord(
                    identity=record.key.identity(),
                    season=season,
                    number=number,
                    watched_at=parse_timestamp(row.get(spec.watched)),
                )
            )
            counted += 1
        note = f"watch history ({counted} episodes)"
        if spec.filename in used:
            # One file can hold both kinds of row; amend rather than duplicate.
            for item in parsed.files:
                if item["file"] == spec.filename:
                    item["used"] += f" + {note}"
                    break
        else:
            parsed.files.append({"file": spec.filename, "rows": len(rows), "used": note})
        used.add(spec.filename)

    ignored = sorted(set(tables) - used)
    if ignored:
        parsed.files.append(
            {
                "file": f"{len(ignored)} other files",
                "rows": 0,
                "used": "ignored (account data, not viewing history)",
                "names": ignored,
            }
        )
    return parsed


# --------------------------------------------------------------------------
# anything that is not a TV Time export
# --------------------------------------------------------------------------


def parse_generic(tables: dict[str, list[dict]]) -> ParsedSource:
    parsed = ParsedSource()
    for name, rows in tables.items():
        if not rows:
            parsed.files.append({"file": name, "rows": 0, "used": "skipped: no rows"})
            continue

        mapping = detect_columns(list(rows[0].keys()))
        if not mapping.has_show:
            parsed.files.append({"file": name, "rows": len(rows), "used": "skipped: no show column"})
            continue
        # A file with no episode columns and only a generic "name" column is far
        # more likely to be account data than a show list.
        generic_name = mapping.show_name in GENERIC_COLUMNS and not mapping.show_id
        if generic_name and not mapping.has_episode:
            parsed.files.append(
                {"file": name, "rows": len(rows), "used": "skipped: no show-specific columns"}
            )
            continue

        kind = "episodes" if mapping.has_episode else "shows"
        counted = 0
        for row in rows:
            key = _show_key(row, mapping.show_id, mapping.show_name)
            if key is None:
                continue
            record = parsed.record(
                key,
                followed=parse_bool(row.get(mapping.followed)) if mapping.followed else None,
                archived=parse_bool(row.get(mapping.archived)) if mapping.archived else None,
            )
            if kind == "episodes":
                season = parse_int(row.get(mapping.season))
                number = parse_int(row.get(mapping.episode))
                if season is None or number is None:
                    continue
                parsed.watches.append(
                    WatchRecord(
                        identity=record.key.identity(),
                        season=season,
                        number=number,
                        watched_at=parse_timestamp(row.get(mapping.watched))
                        if mapping.watched
                        else None,
                    )
                )
            counted += 1
        parsed.files.append(
            {
                "file": name,
                "rows": len(rows),
                "used": f"{kind} ({counted} usable)",
                "columns": mapping.describe(),
            }
        )
    return parsed


def merge_name_only_shows(parsed: ParsedSource) -> int:
    """Fold title-keyed rows into the same show's TVDB-keyed entry.

    Older TV Time tables record only a show's name, newer ones its TVDB id, so
    the same show arrives under two identities. Merging them keeps one entry per
    show, which means one TVmaze lookup and no spurious "unmatched" entries.
    """
    by_title: dict[str, tuple[str, Any]] = {}
    for identity, record in parsed.shows.items():
        if identity[0] == "tvdb" and record.key.name:
            by_title.setdefault(normalize_title(record.key.name), identity)

    remap: dict[tuple[str, Any], tuple[str, Any]] = {}
    for identity, record in list(parsed.shows.items()):
        if identity[0] != "name":
            continue
        target = by_title.get(identity[1])
        if target is None:
            continue
        parsed.shows[target].learn(
            record.key,
            followed=record.followed,
            archived=record.archived,
            favorite=record.favorite,
        )
        remap[identity] = target
        del parsed.shows[identity]

    if remap:
        for watch in parsed.watches:
            watch.identity = remap.get(watch.identity, watch.identity)
    return len(remap)


def parse_source(source: Path) -> ParsedSource:
    tables = load_tables(source)
    parsed = parse_tvtime(tables) if looks_like_tvtime(tables) else parse_generic(tables)
    merged = merge_name_only_shows(parsed)
    if merged:
        parsed.files.append(
            {
                "file": f"{merged} shows",
                "rows": 0,
                "used": "listed under both a title and a TVDB id, merged into one",
            }
        )
    return parsed


# --------------------------------------------------------------------------
# resolving shows against TVmaze
# --------------------------------------------------------------------------


async def resolve_show(key: ShowKey) -> tuple[int | None, str]:
    """Return (tvmaze show id, how we found it)."""
    if key.tvdb_id:
        row = connect().execute(
            "SELECT id FROM show WHERE tvdb_id = ?", (key.tvdb_id,)
        ).fetchone()
        if row:
            return row["id"], "tvdb (cached)"
        try:
            payload = await tvmaze.lookup_by_tvdb(key.tvdb_id)
        except tvmaze.TVmazeError:
            payload = None
        if payload:
            library.save_show(payload)
            return int(payload["id"]), "tvdb"

    if key.name:
        wanted = normalize_title(key.name)
        row = connect().execute(
            "SELECT id FROM show WHERE lower(name) = ?", (key.name.strip().lower(),)
        ).fetchone()
        if row:
            return row["id"], "name (cached)"
        try:
            results = await tvmaze.search_shows(key.name)
        except tvmaze.TVmazeError:
            results = []
        for candidate in results:
            if normalize_title(candidate.get("name") or "") == wanted:
                return int(candidate["id"]), "name (exact)"
        if results:
            return int(results[0]["id"]), "name (best guess)"

    return None, "unmatched"


async def ensure_episodes(show_id: int) -> None:
    row = connect().execute(
        "SELECT COUNT(*) AS n FROM episode WHERE show_id = ?", (show_id,)
    ).fetchone()
    if row["n"] == 0:
        await library.sync_show(show_id)


def episode_index(show_id: int) -> dict[tuple[int, int], int]:
    rows = connect().execute(
        "SELECT id, season, number FROM episode WHERE show_id = ?", (show_id,)
    ).fetchall()
    return {
        (row["season"], row["number"]): row["id"]
        for row in rows
        if row["season"] is not None and row["number"] is not None
    }


# --------------------------------------------------------------------------
# the import itself
# --------------------------------------------------------------------------


async def run_import(
    source: Path,
    *,
    dry_run: bool = False,
    follow_shows: bool = True,
    filename: str | None = None,
    progress: Progress | None = None,
    job_id: int | None = None,
) -> dict:
    """Resolve everything in the export, then (unless dry_run) write it."""

    def report_progress(stage: str, done: int, total: int) -> None:
        if progress is not None:
            progress(stage, done, total)

    report_progress("Reading the export", 0, 0)
    parsed = parse_source(source)

    watched_identities = {watch.identity for watch in parsed.watches}
    resolved: dict[tuple[str, Any], int | None] = {}
    show_report: list[dict] = []

    ordered = sorted(
        parsed.shows.items(),
        key=lambda item: (item[1].key.name or "", item[1].key.tvdb_id or 0),
    )
    total = len(ordered)
    for index, (identity, record) in enumerate(ordered, start=1):
        show_id, how = await resolve_show(record.key)
        resolved[identity] = show_id
        if show_id is not None:
            await ensure_episodes(show_id)
            row = connect().execute("SELECT name FROM show WHERE id = ?", (show_id,)).fetchone()
            show_report.append(
                {
                    "source": record.key.label(),
                    "matched_as": row["name"] if row else None,
                    "show_id": show_id,
                    "how": how,
                }
            )
        else:
            show_report.append(
                {"source": record.key.label(), "matched_as": None, "show_id": None, "how": how}
            )
        report_progress("Matching shows against TVmaze", index, total)

    report_progress("Matching episodes", 0, len(parsed.watches))
    indexes: dict[int, dict[tuple[int, int], int]] = {}
    to_mark: dict[int, tuple[int, str]] = {}  # episode_id -> (show_id, watched_at)
    missing_episodes: list[str] = []
    unnumbered_specials: list[str] = []
    skipped_shows: set[str] = set()

    for watch in parsed.watches:
        show_id = resolved.get(watch.identity)
        record = parsed.shows.get(watch.identity)
        if show_id is None:
            if record is not None:
                skipped_shows.add(record.key.label())
            continue
        if show_id not in indexes:
            indexes[show_id] = episode_index(show_id)
        episode_id = indexes[show_id].get((watch.season, watch.number))
        if episode_id is None:
            label = record.key.label() if record else "unknown show"
            code = f"{label} {library.episode_code(watch.season, watch.number)}"
            # TV Time files specials under season 0 or episode 0, which is a
            # placeholder rather than a number TVmaze can be matched against.
            if watch.season == 0 or watch.number == 0:
                unnumbered_specials.append(code)
            else:
                missing_episodes.append(code)
            continue
        stamp = watch.watched_at or utcnow()
        # Keep the earliest recorded watch when the export lists a rewatch.
        existing = to_mark.get(episode_id)
        if existing is None or stamp < existing[1]:
            to_mark[episode_id] = (show_id, stamp)

    already: set[int] = set()
    if to_mark:
        ids = list(to_mark)
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" for _ in chunk)
            rows = connect().execute(
                f"SELECT episode_id FROM watch WHERE episode_id IN ({marks})", chunk
            ).fetchall()
            already.update(row["episode_id"] for row in rows)
    new_marks = {eid: value for eid, value in to_mark.items() if eid not in already}

    # A show belongs in your library if the export says you follow it, or if you
    # watched it and the export has no opinion. Shows that only ever appeared in
    # a ratings or recommendations table are left out.
    follows: dict[int, dict] = {}
    for identity, record in parsed.shows.items():
        show_id = resolved.get(identity)
        if show_id is None:
            continue
        wanted = record.followed
        if wanted is None:
            wanted = identity in watched_identities
        if not wanted:
            continue
        follows[show_id] = {
            "archived": bool(record.archived),
            "favorite": bool(record.favorite),
        }

    if not dry_run:
        report_progress("Saving", 0, len(new_marks))
        with tx() as conn:
            conn.executemany(
                "INSERT INTO watch (episode_id, show_id, watched_at, source) "
                "VALUES (?, ?, ?, 'import') ON CONFLICT(episode_id) DO NOTHING",
                [(eid, show_id, stamp) for eid, (show_id, stamp) in new_marks.items()],
            )
            if follow_shows:
                stamp = utcnow()
                conn.executemany(
                    "INSERT INTO follow (show_id, followed_at, archived, favorite) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(show_id) DO UPDATE SET "
                    "archived = excluded.archived, favorite = excluded.favorite",
                    [
                        (show_id, stamp, int(flags["archived"]), int(flags["favorite"]))
                        for show_id, flags in follows.items()
                    ],
                )

    report = {
        "dry_run": dry_run,
        "files": parsed.files,
        "format": parsed.format,
        "shows_found": len({s for s in resolved.values() if s}),
        "shows_unmatched": [r["source"] for r in show_report if r["show_id"] is None],
        "shows_guessed": [r["source"] for r in show_report if r.get("how") == "name (best guess)"],
        "shows_to_follow": len([f for f in follows.values() if not f["archived"]]),
        "shows_to_archive": len([f for f in follows.values() if f["archived"]]),
        "watch_rows_read": len(parsed.watches),
        "episodes_marked": len(new_marks),
        "episodes_already_known": len(already),
        "episodes_unmatched": len(missing_episodes),
        "episodes_unmatched_sample": missing_episodes[:50],
        "specials_skipped": len(unnumbered_specials),
        "specials_skipped_sample": unnumbered_specials[:25],
        "shows_skipped_sample": sorted(skipped_shows)[:50],
        "shows": show_report,
    }

    with tx() as conn:
        if job_id is not None:
            conn.execute(
                "UPDATE import_job SET status = ?, report = ?, stage = ?, done = ?, total = ? "
                "WHERE id = ?",
                (
                    "preview" if dry_run else "imported",
                    json.dumps(report),
                    "Finished",
                    total,
                    total,
                    job_id,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO import_job (created_at, filename, dry_run, status, report) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    utcnow(),
                    filename or source.name,
                    1 if dry_run else 0,
                    "preview" if dry_run else "imported",
                    json.dumps(report),
                ),
            )
    return report
