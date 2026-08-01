"""Domain logic: what you follow, what you have watched, and what is next."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import tvmaze
from .db import connect, set_meta, tx, utcnow

TAG_RE = re.compile(r"<[^>]+>")

# Episodes are "available" once their air time has passed.
AIRED = "e.airstamp IS NOT NULL AND e.airstamp <= :now"
# Regular episodes drive progress and up-next; specials are opt-in extras.
MAIN = "e.is_special = 0"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def strip_html(text: str | None) -> str | None:
    if not text:
        return None
    return TAG_RE.sub("", text).replace("&amp;", "&").replace("&nbsp;", " ").strip()


def normalize_airstamp(episode: dict) -> str | None:
    """Return a UTC ISO timestamp so air times compare as plain strings."""
    stamp = episode.get("airstamp")
    if stamp:
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except ValueError:
            pass
    airdate = episode.get("airdate")
    if airdate:
        return f"{airdate}T00:00:00+00:00"
    return None


def is_special(episode: dict) -> bool:
    if episode.get("number") is None:
        return True
    if (episode.get("season") or 0) == 0:
        return True
    return (episode.get("type") or "regular") not in {"regular", "significant_special"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def episode_code(season: int | None, number: int | None) -> str:
    if season is None:
        return "Special"
    if number is None:
        return f"S{season:02d} Special"
    return f"S{season:02d}E{number:02d}"


# --------------------------------------------------------------------------
# writing shows and episodes
# --------------------------------------------------------------------------


def save_show(payload: dict) -> int:
    """Upsert a TVmaze show payload, leaving user data untouched."""
    externals = payload.get("externals") or {}
    network = payload.get("network") or payload.get("webChannel") or {}
    schedule = payload.get("schedule") or {}
    image = payload.get("image") or {}
    row = {
        "id": payload["id"],
        "name": payload.get("name") or "Untitled",
        "status": payload.get("status"),
        "premiered": payload.get("premiered"),
        "ended": payload.get("ended"),
        "network": network.get("name"),
        "language": payload.get("language"),
        "genres": json.dumps(payload.get("genres") or []),
        "runtime": payload.get("runtime"),
        "average_runtime": payload.get("averageRuntime"),
        "image": image.get("medium"),
        "image_original": image.get("original"),
        "summary": strip_html(payload.get("summary")),
        "url": payload.get("url"),
        "tvdb_id": externals.get("thetvdb"),
        "imdb_id": externals.get("imdb"),
        "schedule_time": schedule.get("time"),
        "schedule_days": json.dumps(schedule.get("days") or []),
        "remote_updated": payload.get("updated"),
        "synced_at": utcnow(),
    }
    columns = ", ".join(row)
    placeholders = ", ".join(f":{key}" for key in row)
    updates = ", ".join(f"{key} = excluded.{key}" for key in row if key != "id")
    with tx() as conn:
        conn.execute(
            f"INSERT INTO show ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            row,
        )
    return int(row["id"])


def save_episodes(show_id: int, episodes: Iterable[dict]) -> int:
    rows = []
    seen_ids = []
    for episode in episodes:
        image = episode.get("image") or {}
        rows.append(
            {
                "id": episode["id"],
                "show_id": show_id,
                "season": episode.get("season"),
                "number": episode.get("number"),
                "name": episode.get("name"),
                "type": episode.get("type"),
                "is_special": 1 if is_special(episode) else 0,
                "airdate": episode.get("airdate"),
                "airstamp": normalize_airstamp(episode),
                "runtime": episode.get("runtime"),
                "summary": strip_html(episode.get("summary")),
                "image": image.get("medium"),
                "url": episode.get("url"),
            }
        )
        seen_ids.append(episode["id"])

    with tx() as conn:
        if rows:
            columns = ", ".join(rows[0])
            placeholders = ", ".join(f":{key}" for key in rows[0])
            updates = ", ".join(f"{key} = excluded.{key}" for key in rows[0] if key != "id")
            conn.executemany(
                f"INSERT INTO episode ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                rows,
            )
        # Drop episodes TVmaze no longer lists, but never one you have watched.
        keep = ",".join(str(int(i)) for i in seen_ids) or "0"
        conn.execute(
            f"DELETE FROM episode WHERE show_id = ? AND id NOT IN ({keep}) "
            "AND id NOT IN (SELECT episode_id FROM watch)",
            (show_id,),
        )
    return len(rows)


async def sync_show(show_id: int) -> int:
    """Pull the show and its episode list from TVmaze into the database."""
    payload = await tvmaze.get_show_with_episodes(show_id)
    episodes = (payload.get("_embedded") or {}).get("episodes") or []
    save_show(payload)
    return save_episodes(show_id, episodes)


async def add_show(show_id: int, *, follow: bool = True) -> dict:
    await sync_show(show_id)
    if follow:
        follow_show(show_id)
    return show_detail(show_id)


# --------------------------------------------------------------------------
# following
# --------------------------------------------------------------------------


def follow_show(show_id: int) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO follow (show_id, followed_at) VALUES (?, ?) "
            "ON CONFLICT(show_id) DO UPDATE SET archived = 0",
            (show_id, utcnow()),
        )


def unfollow_show(show_id: int, *, keep_history: bool = True) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM follow WHERE show_id = ?", (show_id,))
        if not keep_history:
            conn.execute("DELETE FROM watch WHERE show_id = ?", (show_id,))


def set_archived(show_id: int, archived: bool) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE follow SET archived = ? WHERE show_id = ?", (1 if archived else 0, show_id)
        )


def set_favorite(show_id: int, favorite: bool) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE follow SET favorite = ? WHERE show_id = ?", (1 if favorite else 0, show_id)
        )


def set_priority(show_id: int, priority: bool) -> None:
    """Priority shows are pinned to the top of Up Next when they have something
    to watch. Favourites are a permanent label; priority is 'get to this next'."""
    with tx() as conn:
        conn.execute(
            "UPDATE follow SET priority = ? WHERE show_id = ?", (1 if priority else 0, show_id)
        )


# --------------------------------------------------------------------------
# watching
# --------------------------------------------------------------------------


def mark_watched(episode_id: int, watched_at: str | None = None, source: str = "app") -> None:
    with tx() as conn:
        row = conn.execute("SELECT show_id FROM episode WHERE id = ?", (episode_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown episode {episode_id}")
        conn.execute(
            "INSERT INTO watch (episode_id, show_id, watched_at, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(episode_id) DO UPDATE SET watched_at = excluded.watched_at",
            (episode_id, row["show_id"], watched_at or utcnow(), source),
        )


def unmark_watched(episode_id: int) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM watch WHERE episode_id = ?", (episode_id,))


def _order_key(row: sqlite3.Row | dict) -> tuple[int, int]:
    season = row["season"] if row["season"] is not None else 0
    number = row["number"] if row["number"] is not None else 0
    return season, number


def mark_through(show_id: int, episode_id: int, include_specials: bool = False) -> int:
    """Mark the given episode and everything before it. The 'I binged it' button."""
    conn = connect()
    target = conn.execute(
        "SELECT season, number FROM episode WHERE id = ? AND show_id = ?", (episode_id, show_id)
    ).fetchone()
    if target is None:
        raise LookupError(f"episode {episode_id} is not part of show {show_id}")

    clause = "" if include_specials else " AND is_special = 0"
    rows = conn.execute(
        f"SELECT id, season, number FROM episode WHERE show_id = ?{clause}", (show_id,)
    ).fetchall()
    limit = _order_key(target)
    pending = [row["id"] for row in rows if _order_key(row) <= limit]

    stamp = utcnow()
    with tx() as conn:
        conn.executemany(
            "INSERT INTO watch (episode_id, show_id, watched_at, source) VALUES (?, ?, ?, 'bulk') "
            "ON CONFLICT(episode_id) DO NOTHING",
            [(episode_id_, show_id, stamp) for episode_id_ in pending],
        )
    return len(pending)


def set_season_watched(show_id: int, season: int, watched: bool) -> int:
    conn = connect()
    rows = conn.execute(
        "SELECT id FROM episode WHERE show_id = ? AND season = ? AND is_special = 0",
        (show_id, season),
    ).fetchall()
    ids = [row["id"] for row in rows]
    stamp = utcnow()
    with tx() as conn:
        if watched:
            conn.executemany(
                "INSERT INTO watch (episode_id, show_id, watched_at, source) "
                "VALUES (?, ?, ?, 'bulk') ON CONFLICT(episode_id) DO NOTHING",
                [(episode_id, show_id, stamp) for episode_id in ids],
            )
        elif ids:
            marks = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM watch WHERE episode_id IN ({marks})", ids)
    return len(ids)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def _show_row(show_id: int) -> sqlite3.Row | None:
    return connect().execute("SELECT * FROM show WHERE id = ?", (show_id,)).fetchone()


def show_public(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["genres"] = json.loads(data.get("genres") or "[]")
    data["schedule_days"] = json.loads(data.get("schedule_days") or "[]")
    return data


def episode_public(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["code"] = episode_code(data.get("season"), data.get("number"))
    data["watched"] = bool(data.get("watched_at"))
    data["aired"] = bool(data.get("airstamp") and data["airstamp"] <= now_iso())
    return data


def progress(show_id: int) -> dict:
    conn = connect()
    row = conn.execute(
        f"""
        SELECT
          COUNT(*) FILTER (WHERE {MAIN})                             AS total,
          COUNT(*) FILTER (WHERE {MAIN} AND {AIRED})                 AS aired,
          COUNT(w.episode_id) FILTER (WHERE {MAIN})                  AS watched,
          COALESCE(SUM(COALESCE(e.runtime, s.average_runtime, s.runtime, 0))
                   FILTER (WHERE w.episode_id IS NOT NULL), 0)       AS minutes
        FROM episode e
        JOIN show s ON s.id = e.show_id
        LEFT JOIN watch w ON w.episode_id = e.id
        WHERE e.show_id = :show_id
        """,
        {"show_id": show_id, "now": now_iso()},
    ).fetchone()
    total, aired, watched = row["total"] or 0, row["aired"] or 0, row["watched"] or 0
    return {
        "total": total,
        "aired": aired,
        "watched": watched,
        "remaining": max(aired - watched, 0),
        "minutes": row["minutes"] or 0,
        "percent": round(100 * watched / total) if total else 0,
    }


def next_episode(show_id: int) -> sqlite3.Row | None:
    """The first unwatched regular episode, i.e. the first gap in your run."""
    return connect().execute(
        f"""
        SELECT e.* FROM episode e
        LEFT JOIN watch w ON w.episode_id = e.id
        WHERE e.show_id = ? AND {MAIN} AND w.episode_id IS NULL
        ORDER BY e.season, e.number
        LIMIT 1
        """,
        (show_id,),
    ).fetchone()


def last_watched_episode(show_id: int) -> sqlite3.Row | None:
    """What you most recently watched, by when you watched it."""
    return connect().execute(
        """
        SELECT e.*, w.watched_at FROM watch w
        JOIN episode e ON e.id = w.episode_id
        WHERE w.show_id = ?
        ORDER BY w.watched_at DESC, e.season DESC, e.number DESC
        LIMIT 1
        """,
        (show_id,),
    ).fetchone()


def gaps_before_furthest(show_id: int) -> int:
    """Unwatched episodes that sit behind the furthest point you have reached.

    Watching a show out of order — dipping into a long-running series, or
    starting at a later season — leaves holes earlier in the run. Up-next points
    at the first of those holes, which is right, but only makes sense if you can
    see how many there are.
    """
    conn = connect()
    furthest = conn.execute(
        """
        SELECT e.season AS season, e.number AS number FROM watch w
        JOIN episode e ON e.id = w.episode_id
        WHERE w.show_id = ? AND e.is_special = 0
        ORDER BY e.season DESC, e.number DESC
        LIMIT 1
        """,
        (show_id,),
    ).fetchone()
    if furthest is None:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM episode e
        LEFT JOIN watch w ON w.episode_id = e.id
        WHERE e.show_id = ? AND e.is_special = 0 AND w.episode_id IS NULL
          AND (e.season < ? OR (e.season = ? AND e.number < ?))
        """,
        (show_id, furthest["season"], furthest["season"], furthest["number"]),
    ).fetchone()
    return row["n"] or 0


def _status_for(show: sqlite3.Row, nxt: sqlite3.Row | None, prog: dict) -> str:
    if nxt is None:
        return "complete" if (show["status"] or "") == "Ended" else "caught_up"
    if nxt["airstamp"] and nxt["airstamp"] <= now_iso():
        return "ready"
    if nxt["airstamp"]:
        return "scheduled"
    return "unscheduled"


def show_card(show_id: int) -> dict | None:
    row = _show_row(show_id)
    if row is None:
        return None
    prog = progress(show_id)
    nxt = next_episode(show_id)
    last = last_watched_episode(show_id)
    follow = connect().execute("SELECT * FROM follow WHERE show_id = ?", (show_id,)).fetchone()
    return {
        "show": show_public(row),
        "progress": prog,
        "next": episode_public(nxt) if nxt else None,
        "last_watched": episode_public(last) if last else None,
        "status": _status_for(row, nxt, prog),
        "started": prog["watched"] > 0,
        "gaps": gaps_before_furthest(show_id) if prog["watched"] else 0,
        "following": follow is not None,
        "archived": bool(follow["archived"]) if follow else False,
        "favorite": bool(follow["favorite"]) if follow else False,
        "priority": bool(follow["priority"]) if follow else False,
    }


def followed_ids(include_archived: bool = False) -> list[int]:
    clause = "" if include_archived else " WHERE archived = 0"
    rows = connect().execute(f"SELECT show_id FROM follow{clause}").fetchall()
    return [row["show_id"] for row in rows]


def home() -> dict:
    """The main screen: everything grouped by what you can do with it."""
    cards = [card for card in (show_card(sid) for sid in followed_ids()) if card]

    # Priority shows jump the queue, but only while they actually have an aired
    # episode waiting — a pinned show you are caught up on is not actionable.
    priority = sorted(
        [c for c in cards if c["status"] == "ready" and c["priority"]],
        key=lambda c: (
            c["started"],
            (c["last_watched"] or {}).get("watched_at") or "",
        ),
        reverse=True,
    )
    # A show you are part-way through is the thing you actually want to resume,
    # so it is ordered by when you last watched it. Shows you follow but have
    # never started would otherwise bury them, so they get their own section.
    ready = sorted(
        [c for c in cards if c["status"] == "ready" and c["started"] and not c["priority"]],
        key=lambda c: (c["last_watched"] or {}).get("watched_at") or "",
        reverse=True,
    )
    not_started = sorted(
        [c for c in cards if c["status"] == "ready" and not c["started"] and not c["priority"]],
        key=lambda c: (c["next"] or {}).get("airstamp") or "",
        reverse=True,
    )
    scheduled = sorted(
        [c for c in cards if c["status"] == "scheduled"],
        key=lambda c: (c["next"] or {}).get("airstamp") or "",
    )
    waiting = sorted(
        [c for c in cards if c["status"] in {"caught_up", "unscheduled"}],
        key=lambda c: (c["last_watched"] or {}).get("watched_at") or "",
        reverse=True,
    )
    complete = sorted(
        [c for c in cards if c["status"] == "complete"],
        key=lambda c: (c["last_watched"] or {}).get("watched_at") or "",
        reverse=True,
    )
    return {
        "priority": priority,
        "ready": ready,
        "not_started": not_started,
        "scheduled": scheduled,
        "waiting": waiting,
        "complete": complete,
        "counts": {
            "priority": len(priority),
            "ready": len(ready),
            "not_started": len(not_started),
            "scheduled": len(scheduled),
            "waiting": len(waiting),
            "complete": len(complete),
            "episodes_ready": sum(
                c["progress"]["remaining"] for c in (*priority, *ready)
            ),
        },
    }


def show_detail(show_id: int) -> dict | None:
    card = show_card(show_id)
    if card is None:
        return None
    rows = connect().execute(
        """
        SELECT e.*, w.watched_at FROM episode e
        LEFT JOIN watch w ON w.episode_id = e.id
        WHERE e.show_id = ?
        ORDER BY e.season, (e.number IS NULL), e.number
        """,
        (show_id,),
    ).fetchall()

    seasons: dict[int, dict] = {}
    for row in rows:
        episode = episode_public(row)
        season = episode["season"] if episode["season"] is not None else 0
        bucket = seasons.setdefault(
            season, {"season": season, "episodes": [], "watched": 0, "total": 0, "aired": 0}
        )
        bucket["episodes"].append(episode)
        if not episode["is_special"]:
            bucket["total"] += 1
            bucket["aired"] += 1 if episode["aired"] else 0
            bucket["watched"] += 1 if episode["watched"] else 0

    card["seasons"] = [seasons[key] for key in sorted(seasons)]
    return card


def upcoming(days: int = 14, include_unfollowed: bool = False) -> list[dict]:
    """Airings for followed shows within the window, soonest first."""
    start = now_iso()
    end = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
    join = "" if include_unfollowed else "JOIN follow f ON f.show_id = e.show_id AND f.archived = 0"
    rows = connect().execute(
        f"""
        SELECT e.*, s.name AS show_name, s.image AS show_image, s.network
        FROM episode e
        JOIN show s ON s.id = e.show_id
        {join}
        WHERE e.airstamp > ? AND e.airstamp <= ?
        ORDER BY e.airstamp
        """,
        (start, end),
    ).fetchall()
    return [episode_public(row) for row in rows]


def new_since(since: str | None, limit: int = 60) -> list[dict]:
    """Episodes of followed shows that aired since a timestamp and are unwatched."""
    if not since:
        since = (datetime.now(timezone.utc) - timedelta(days=7)).replace(microsecond=0).isoformat()
    rows = connect().execute(
        """
        SELECT e.*, s.name AS show_name, s.image AS show_image, s.network
        FROM episode e
        JOIN show s ON s.id = e.show_id
        JOIN follow f ON f.show_id = e.show_id AND f.archived = 0
        LEFT JOIN watch w ON w.episode_id = e.id
        WHERE e.is_special = 0 AND w.episode_id IS NULL
          AND e.airstamp > ? AND e.airstamp <= ?
        ORDER BY e.airstamp DESC
        LIMIT ?
        """,
        (since, now_iso(), limit),
    ).fetchall()
    return [episode_public(row) for row in rows]


def history(limit: int = 100, offset: int = 0) -> list[dict]:
    rows = connect().execute(
        """
        SELECT e.*, w.watched_at, w.source, s.name AS show_name, s.image AS show_image
        FROM watch w
        JOIN episode e ON e.id = w.episode_id
        JOIN show s ON s.id = w.show_id
        ORDER BY w.watched_at DESC, e.season DESC, e.number DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return [episode_public(row) for row in rows]


def stats() -> dict:
    conn = connect()
    totals = conn.execute(
        """
        SELECT COUNT(*) AS episodes,
               COALESCE(SUM(COALESCE(e.runtime, s.average_runtime, s.runtime, 0)), 0) AS minutes,
               COUNT(DISTINCT w.show_id) AS shows
        FROM watch w
        JOIN episode e ON e.id = w.episode_id
        JOIN show s ON s.id = w.show_id
        """
    ).fetchone()
    recent = conn.execute(
        """
        SELECT substr(w.watched_at, 1, 7) AS month, COUNT(*) AS episodes
        FROM watch w GROUP BY month ORDER BY month DESC LIMIT 12
        """
    ).fetchall()
    top = conn.execute(
        """
        SELECT s.id, s.name, s.image, COUNT(*) AS episodes,
               COALESCE(SUM(COALESCE(e.runtime, s.average_runtime, s.runtime, 0)), 0) AS minutes
        FROM watch w
        JOIN episode e ON e.id = w.episode_id
        JOIN show s ON s.id = w.show_id
        GROUP BY s.id ORDER BY minutes DESC LIMIT 10
        """
    ).fetchall()
    following = conn.execute(
        "SELECT COUNT(*) AS n FROM follow WHERE archived = 0"
    ).fetchone()["n"]
    return {
        "episodes": totals["episodes"] or 0,
        "minutes": totals["minutes"] or 0,
        "shows": totals["shows"] or 0,
        "following": following,
        "by_month": [dict(row) for row in recent],
        "top_shows": [dict(row) for row in top],
    }


def touch_last_seen() -> str:
    """Record 'you have looked at the app now' for the new-episode badge."""
    previous = connect().execute(
        "SELECT value FROM meta WHERE key = 'last_seen_at'"
    ).fetchone()
    set_meta("last_seen_at", now_iso())
    return previous["value"] if previous else ""


async def search(query: str) -> list[dict]:
    """Search TVmaze and flag which results you already follow."""
    results = await tvmaze.search_shows(query)
    if not results:
        return []
    ids = [str(int(show["id"])) for show in results]
    rows = connect().execute(
        f"SELECT show_id, archived FROM follow WHERE show_id IN ({','.join(ids)})"
    ).fetchall()
    following = {row["show_id"]: row for row in rows}
    output = []
    for show in results:
        image = show.get("image") or {}
        network = show.get("network") or show.get("webChannel") or {}
        output.append(
            {
                "id": show["id"],
                "name": show.get("name"),
                "premiered": show.get("premiered"),
                "ended": show.get("ended"),
                "status": show.get("status"),
                "network": network.get("name"),
                "image": image.get("medium"),
                "summary": strip_html(show.get("summary")),
                "genres": show.get("genres") or [],
                "following": show["id"] in following,
            }
        )
    return output
