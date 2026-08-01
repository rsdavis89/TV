"""Import a TV Time data export (or any comparable CSV) into the library.

TV Time's GDPR export has changed shape over the years, and the archive people
actually received differs between accounts. So instead of hard-coding one
schema this module sniffs each CSV's header, works out which columns hold the
show, the season, the episode and the watch date, and imports whatever it can
recognise. Anything it cannot match is listed in the report rather than
silently dropped.
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
from typing import Any, Iterable, Sequence

from . import library, tvmaze
from .db import connect, tx, utcnow

# Column name candidates, most specific first. Headers are normalised to
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

YEAR_SUFFIX = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")
NON_ALNUM = re.compile(r"[^a-z0-9]+")
DIGITS = re.compile(r"\d+")


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
    """Exact match first, then a suffix/contains match, honouring priority."""
    lookup = set(headers)
    for candidate in candidates:
        if candidate in lookup:
            return candidate
    for candidate in candidates:
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
        }


def detect_columns(headers: Sequence[str]) -> ColumnMap:
    normalized = [normalize_header(header) for header in headers]
    mapping = ColumnMap(
        show_id=pick_column(normalized, SHOW_ID_COLUMNS),
        show_name=pick_column(normalized, SHOW_NAME_COLUMNS),
        season=pick_column(normalized, SEASON_COLUMNS),
        episode=pick_column(normalized, EPISODE_COLUMNS),
        watched=pick_column(normalized, WATCHED_COLUMNS),
    )
    # "episode_id" is an identifier, never an episode number.
    if mapping.episode and mapping.episode.endswith("_id"):
        mapping.episode = None
    if mapping.show_name and mapping.show_name == mapping.show_id:
        mapping.show_name = None
    return mapping


@dataclass(frozen=True)
class ShowKey:
    tvdb_id: int | None
    name: str | None

    def label(self) -> str:
        if self.name and self.tvdb_id:
            return f"{self.name} (tvdb {self.tvdb_id})"
        if self.name:
            return self.name
        return f"tvdb {self.tvdb_id}"


@dataclass
class WatchRecord:
    key: ShowKey
    season: int
    number: int
    watched_at: str | None


@dataclass
class ParsedSource:
    watches: list[WatchRecord] = field(default_factory=list)
    follows: set[ShowKey] = field(default_factory=set)
    files: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------
# reading the export
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


def parse_source(source: Path) -> ParsedSource:
    parsed = ParsedSource()
    for name, blob in iter_tables(source):
        rows = read_rows(name, blob)
        if not rows:
            parsed.files.append({"file": name, "rows": 0, "used": "skipped: no rows"})
            continue

        headers = list(rows[0].keys())
        mapping = detect_columns(headers)
        if not mapping.has_show:
            parsed.files.append(
                {"file": name, "rows": len(rows), "used": "skipped: no show column"}
            )
            continue

        normalized_rows = [
            {normalize_header(key): value for key, value in row.items()} for row in rows
        ]
        kind = "episodes" if mapping.has_episode else "shows"
        added = 0
        for row in normalized_rows:
            tvdb_id = parse_int(row.get(mapping.show_id)) if mapping.show_id else None
            raw_name = (row.get(mapping.show_name) or "").strip() if mapping.show_name else ""
            key = ShowKey(tvdb_id=tvdb_id or None, name=raw_name or None)
            if key.tvdb_id is None and key.name is None:
                continue
            parsed.follows.add(key)
            if kind == "episodes":
                season = parse_int(row.get(mapping.season))
                number = parse_int(row.get(mapping.episode))
                if season is None or number is None:
                    continue
                parsed.watches.append(
                    WatchRecord(
                        key=key,
                        season=season,
                        number=number,
                        watched_at=parse_timestamp(row.get(mapping.watched))
                        if mapping.watched
                        else None,
                    )
                )
            added += 1
        parsed.files.append(
            {
                "file": name,
                "rows": len(rows),
                "used": f"{kind} ({added} usable)",
                "columns": mapping.describe(),
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
            return int(payload["id"]), "tvdb"

    if key.name:
        wanted = normalize_title(key.name)
        row = connect().execute(
            "SELECT id, name FROM show WHERE lower(name) = ?", (key.name.strip().lower(),)
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
    """Sync a show's episodes once per import run."""
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


async def run_import(
    source: Path,
    *,
    dry_run: bool = False,
    follow_shows: bool = True,
    filename: str | None = None,
) -> dict:
    parsed = parse_source(source)

    resolved: dict[ShowKey, int | None] = {}
    show_report: list[dict] = []
    for key in sorted(parsed.follows, key=lambda k: (k.name or "", k.tvdb_id or 0)):
        show_id, how = await resolve_show(key)
        resolved[key] = show_id
        if show_id is not None:
            await ensure_episodes(show_id)
            row = connect().execute("SELECT name FROM show WHERE id = ?", (show_id,)).fetchone()
            show_report.append(
                {
                    "source": key.label(),
                    "matched_as": row["name"] if row else None,
                    "show_id": show_id,
                    "how": how,
                    "confident": how != "name (best guess)",
                }
            )
        else:
            show_report.append(
                {"source": key.label(), "matched_as": None, "show_id": None, "how": how}
            )

    indexes: dict[int, dict[tuple[int, int], int]] = {}
    to_mark: dict[int, tuple[int, str]] = {}  # episode_id -> (show_id, watched_at)
    missing_episodes: list[str] = []
    skipped_shows: set[str] = set()

    for record in parsed.watches:
        show_id = resolved.get(record.key)
        if show_id is None:
            skipped_shows.add(record.key.label())
            continue
        if show_id not in indexes:
            indexes[show_id] = episode_index(show_id)
        episode_id = indexes[show_id].get((record.season, record.number))
        if episode_id is None:
            missing_episodes.append(
                f"{record.key.label()} "
                f"{library.episode_code(record.season, record.number)}"
            )
            continue
        stamp = record.watched_at or utcnow()
        # Keep the earliest recorded watch when the export lists an episode twice.
        existing = to_mark.get(episode_id)
        if existing is None or stamp < existing[1]:
            to_mark[episode_id] = (show_id, stamp)

    already = set()
    if to_mark:
        marks = ",".join("?" for _ in to_mark)
        rows = connect().execute(
            f"SELECT episode_id FROM watch WHERE episode_id IN ({marks})", list(to_mark)
        ).fetchall()
        already = {row["episode_id"] for row in rows}

    new_marks = {eid: value for eid, value in to_mark.items() if eid not in already}

    if not dry_run:
        with tx() as conn:
            conn.executemany(
                "INSERT INTO watch (episode_id, show_id, watched_at, source) "
                "VALUES (?, ?, ?, 'import') ON CONFLICT(episode_id) DO NOTHING",
                [(eid, show_id, stamp) for eid, (show_id, stamp) in new_marks.items()],
            )
            if follow_shows:
                stamp = utcnow()
                conn.executemany(
                    "INSERT INTO follow (show_id, followed_at) VALUES (?, ?) "
                    "ON CONFLICT(show_id) DO NOTHING",
                    [(sid, stamp) for sid in {s for s in resolved.values() if s}],
                )

    report = {
        "dry_run": dry_run,
        "files": parsed.files,
        "shows_found": len({s for s in resolved.values() if s}),
        "shows_unmatched": [r["source"] for r in show_report if r["show_id"] is None],
        "shows_guessed": [r["source"] for r in show_report if r.get("how") == "name (best guess)"],
        "shows": show_report,
        "watch_rows_read": len(parsed.watches),
        "episodes_marked": len(new_marks),
        "episodes_already_known": len(already),
        "episodes_unmatched": len(missing_episodes),
        "episodes_unmatched_sample": missing_episodes[:50],
        "shows_skipped_sample": sorted(skipped_shows)[:50],
    }

    with tx() as conn:
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
