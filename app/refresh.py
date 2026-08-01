"""Background refresh: keep episode lists current so new seasons show up."""

from __future__ import annotations

import asyncio
import json
import logging

from . import config, library, tvmaze
from .db import connect, get_meta, set_meta, utcnow

log = logging.getLogger("tv.refresh")

_lock = asyncio.Lock()


def _stale_cutoff() -> str:
    from datetime import datetime, timedelta, timezone

    return (
        (datetime.now(timezone.utc) - timedelta(hours=config.STALE_SHOW_HOURS))
        .replace(microsecond=0)
        .isoformat()
    )


async def refresh_all(force: bool = False) -> dict:
    """Re-sync followed shows whose TVmaze record changed since we last looked."""
    async with _lock:
        show_ids = library.followed_ids(include_archived=True)
        if not show_ids:
            report = {"checked": 0, "synced": 0, "new_episodes": 0, "at": utcnow()}
            set_meta("last_refresh_at", report["at"])
            set_meta("last_refresh", json.dumps(report))
            return report

        updates: dict[str, int] = {}
        if not force:
            try:
                updates = await tvmaze.updates_since("week")
            except tvmaze.TVmazeError as exc:
                log.warning("could not fetch TVmaze updates feed: %s", exc)

        cutoff = _stale_cutoff()
        conn = connect()
        pending: list[int] = []
        for show_id in show_ids:
            row = conn.execute(
                "SELECT remote_updated, synced_at FROM show WHERE id = ?", (show_id,)
            ).fetchone()
            if row is None or force:
                pending.append(show_id)
                continue
            remote = updates.get(str(show_id))
            if remote is not None and remote != (row["remote_updated"] or 0):
                pending.append(show_id)
            elif not row["synced_at"] or row["synced_at"] < cutoff:
                pending.append(show_id)

        before = _episode_counts(pending)
        synced, failed = 0, []
        for show_id in pending:
            try:
                await library.sync_show(show_id)
                synced += 1
            except tvmaze.TVmazeError as exc:
                log.warning("refresh failed for show %s: %s", show_id, exc)
                failed.append(show_id)
        after = _episode_counts(pending)

        forgotten = library.forget_unused_shows()
        if forgotten:
            log.info("cleared %d show(s) that were previewed but never added", forgotten)

        report = {
            "at": utcnow(),
            "checked": len(show_ids),
            "synced": synced,
            "failed": failed,
            "new_episodes": max(sum(after.values()) - sum(before.values()), 0),
        }
        set_meta("last_refresh_at", report["at"])
        set_meta("last_refresh", json.dumps(report))
        return report


def _episode_counts(show_ids: list[int]) -> dict[int, int]:
    if not show_ids:
        return {}
    marks = ",".join("?" for _ in show_ids)
    rows = connect().execute(
        f"SELECT show_id, COUNT(*) AS n FROM episode WHERE show_id IN ({marks}) GROUP BY show_id",
        show_ids,
    ).fetchall()
    return {row["show_id"]: row["n"] for row in rows}


def last_refresh() -> dict:
    raw = get_meta("last_refresh")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


async def scheduler() -> None:
    """Loop forever, refreshing on the configured interval."""
    if config.REFRESH_ON_START:
        try:
            await refresh_all()
        except Exception:  # a failed refresh must not kill the loop
            log.exception("startup refresh failed")
    interval = max(config.REFRESH_INTERVAL_HOURS, 1) * 3600
    while True:
        await asyncio.sleep(interval)
        try:
            await refresh_all()
        except Exception:
            log.exception("scheduled refresh failed")
