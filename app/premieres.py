"""New shows and returning seasons, so there is somewhere to find things to add.

Up Next only ever knows about shows you already follow, which leaves the
question of how anything gets onto that list in the first place. This walks
TVmaze's schedule looking for first episodes — season 1 episode 1 is a brand new
show, any other season's first episode is a returning one — and keeps them in a
small table the New tab can read instantly.

Unfiltered this is about thirty English premieres a week, most of it food
programming, true crime and sport. Filtering to a handful of services is what
makes it readable, so the app ships with a default set and the rest are there to
switch on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from . import tvmaze
from .db import connect, get_meta, set_meta, tx, utcnow
from .library import strip_html

log = logging.getLogger("tv.premieres")

# Names exactly as TVmaze spells them, confirmed against six weeks of schedule.
# HBO appears under both its network and its streaming name.
DEFAULT_SERVICES = [
    "Netflix",
    "Prime Video",
    "HBO",
    "HBO Max",
    "Apple TV",
    "Paramount+",
    "Peacock",
    "Hulu",
    "Disney+",
    "AMC+",
    "MGM+",
]

# Offered alongside the defaults, grouped so the list is navigable rather than
# eighty alphabetical checkboxes.
SERVICE_GROUPS = {
    "Streaming": [
        "Netflix", "Prime Video", "HBO", "HBO Max", "Apple TV", "Paramount+",
        "Peacock", "Hulu", "Disney+", "AMC+", "MGM+", "STARZ", "Showtime",
        "Shudder", "Sundance Now", "Acorn TV", "BritBox", "ALLBLK", "Hallmark+",
        "Tubi", "The Roku Channel", "Crave", "Dropout",
    ],
    "US broadcast and cable": [
        "ABC", "CBS", "NBC", "FOX", "The CW", "PBS", "FX", "AMC", "TNT", "TBS",
        "Syfy", "Bravo", "Freeform", "Adult Swim", "A&E", "History", "Discovery",
        "National Geographic", "Vice TV", "WE tv", "TLC", "Nickelodeon",
        "Disney Channel", "Smithsonian Channel",
    ],
    "UK and Ireland": ["BBC iPlayer", "ITVX", "Channel 4", "Sky", "U", "Now"],
    "Unscripted and sport": [
        "Food Network", "HGTV", "Investigation Discovery", "Oxygen True Crime",
        "MotorTrend", "REELZ", "Fox Nation", "ESPN+", "ESPN2", "NFL Network",
        "NBA TV", "SEC Network", "Oprah Winfrey Network", "CNN",
    ],
}

WINDOW_BACK_DAYS = 14
WINDOW_AHEAD_DAYS = 21
# One sweep is ~30 requests, one of them a 10 MB payload, so do not repeat it often.
MIN_HOURS_BETWEEN_SWEEPS = 12


def _show_of(item: dict) -> dict:
    return (item.get("_embedded") or {}).get("show") or item.get("show") or {}


def _channel_of(show: dict) -> str | None:
    channel = show.get("webChannel") or show.get("network") or {}
    return channel.get("name")


def _normalise(item: dict) -> dict | None:
    """Turn a schedule entry into a premiere row, or None if it is not one."""
    if item.get("number") != 1:
        return None
    show = _show_of(item)
    if not show or show.get("language") != "English":
        return None
    channel = _channel_of(show)
    if not channel:
        return None

    season = item.get("season")
    airstamp = item.get("airstamp") or (
        f"{item['airdate']}T00:00:00+00:00" if item.get("airdate") else None
    )
    if not airstamp:
        return None

    image = (show.get("image") or {}) or (item.get("image") or {})
    return {
        "episode_id": item["id"],
        "show_id": show["id"],
        "show_name": show.get("name"),
        "season": season,
        "airstamp": airstamp,
        "channel": channel,
        "kind": "series" if season == 1 else "season",
        "genres": json.dumps(show.get("genres") or []),
        "summary": strip_html(show.get("summary")),
        "image": image.get("medium"),
        "show_status": show.get("status"),
        "rating": (show.get("rating") or {}).get("average"),
        "runtime": show.get("averageRuntime") or show.get("runtime"),
        "fetched_at": utcnow(),
    }


async def sweep(back_days: int = WINDOW_BACK_DAYS, ahead_days: int | None = None) -> dict:
    """Record every premiere: one call for the future, day by day for the past.

    The past needs a request per day per schedule, but everything still to come
    arrives in a single /schedule/full, which is both far cheaper than 40-odd
    daily requests and unlimited in horizon.
    """
    today = datetime.now(timezone.utc).date()
    rows: dict[int, dict] = {}
    failures = 0

    try:
        for item in await tvmaze.full_schedule():
            row = _normalise(item)
            if row:
                rows[row["episode_id"]] = row
    except Exception:  # a large response with its own ways to fail
        log.warning("full schedule fetch failed; falling back to daily requests")
        failures += 1

    for offset in range(-abs(back_days), 1):
        day = (today + timedelta(days=offset)).isoformat()
        for path, params in (
            ("/schedule/web", {"date": day}),
            ("/schedule", {"date": day, "country": "US"}),
        ):
            try:
                items = await tvmaze._get(path, params) or []
            except tvmaze.TVmazeError:
                failures += 1
                continue
            for item in items:
                row = _normalise(item)
                if row:
                    rows[row["episode_id"]] = row

    if rows:
        columns = ", ".join(next(iter(rows.values())))
        placeholders = ", ".join(f":{key}" for key in next(iter(rows.values())))
        updates = ", ".join(
            f"{key} = excluded.{key}" for key in next(iter(rows.values())) if key != "episode_id"
        )
        with tx() as conn:
            conn.executemany(
                f"INSERT INTO premiere ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(episode_id) DO UPDATE SET {updates}",
                list(rows.values()),
            )

    removed = prune()
    set_meta("premieres_swept_at", utcnow())
    report = {"found": len(rows), "removed": removed, "failed_requests": failures, "at": utcnow()}
    set_meta("premieres_last_report", json.dumps(report))
    log.info("premiere sweep: %s", report)
    return report


def prune(keep_days: int = 120) -> int:
    """Drop premieres that have aged out of any window we would show."""
    cutoff = (
        (datetime.now(timezone.utc) - timedelta(days=keep_days)).replace(microsecond=0).isoformat()
    )
    with tx() as conn:
        cursor = conn.execute("DELETE FROM premiere WHERE airstamp < ?", (cutoff,))
        return max(cursor.rowcount, 0)


def due_for_sweep() -> bool:
    last = get_meta("premieres_swept_at")
    if not last:
        return True
    cutoff = (
        (datetime.now(timezone.utc) - timedelta(hours=MIN_HOURS_BETWEEN_SWEEPS))
        .replace(microsecond=0)
        .isoformat()
    )
    return last < cutoff


def listing(back_days: int = 14, ahead_days: int = 21, include_followed: bool = False) -> dict:
    """Premieres in the window, newest first, flagged against your library."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=max(back_days, 0))).replace(microsecond=0).isoformat()
    end = (now + timedelta(days=max(ahead_days, 0))).replace(microsecond=0).isoformat()

    rows = connect().execute(
        """
        SELECT p.*, f.show_id IS NOT NULL AS following
        FROM premiere p
        LEFT JOIN follow f ON f.show_id = p.show_id
        WHERE p.airstamp >= ? AND p.airstamp <= ?
        ORDER BY p.airstamp DESC
        """,
        (start, end),
    ).fetchall()

    items = []
    channels: dict[str, int] = {}
    for row in rows:
        item = dict(row)
        item["genres"] = json.loads(item.get("genres") or "[]")
        item["following"] = bool(item["following"])
        item["aired"] = item["airstamp"] <= now.replace(microsecond=0).isoformat()
        channels[item["channel"]] = channels.get(item["channel"], 0) + 1
        # A returning season of something you already follow is not a discovery;
        # your own episode tracking already has it.
        if item["following"] and not include_followed:
            continue
        items.append(item)

    return {
        "premieres": items,
        "channels": sorted(channels.items(), key=lambda pair: (-pair[1], pair[0])),
        "defaults": DEFAULT_SERVICES,
        "groups": SERVICE_GROUPS,
        "swept_at": get_meta("premieres_swept_at"),
        "stored": connect().execute("SELECT COUNT(*) AS n FROM premiere").fetchone()["n"],
        "window": {"back_days": back_days, "ahead_days": ahead_days},
    }


async def scheduler() -> None:
    """Sweep on startup if due, then twice a day."""
    while True:
        try:
            if due_for_sweep():
                await sweep()
        except Exception:
            log.exception("premiere sweep failed")
        await asyncio.sleep(MIN_HOURS_BETWEEN_SWEEPS * 3600)
