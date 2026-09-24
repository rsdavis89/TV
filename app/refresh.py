"""Background refresh: keep episode lists current so new seasons show up."""

from __future__ import annotations

import asyncio
import json
import logging

from . import config, library, tvmaze
from .db import connect, get_meta, set_meta, tx, utcnow

log = logging.getLogger("tv.refresh")

_lock = asyncio.Lock()

# Bump this when a release changes what a sync collects, and every followed
# show is re-synced once on the next pass regardless of staleness. The pass
# below otherwise skips any show synced within the week, so a fix that changes
# what get_show_with_episodes returns - specials, most recently - would reach a
# 405-show library one show at a time over seven days, and only as TVmaze
# happened to flag each one. Premieres do the same with SWEEP_GENERATION.
SYNC_GENERATION = "2"


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

        # A generation change means the code now collects something it did
        # not before, so nothing already on record can be trusted as complete.
        regenerate = get_meta("episode_sync_generation") != SYNC_GENERATION
        force = force or regenerate

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
            except Exception:
                # Anything else - a garbled payload, a record missing a field -
                # is still one show's problem. Letting it escape abandoned every
                # show after it, and during a regeneration pass left the
                # generation unrecorded, so each pass repeated the whole library
                # and died at the same show.
                log.exception("refresh failed for show %s", show_id)
                failed.append(show_id)
        if failed:
            # A show that did not sync must not count as fresh, or staleness
            # skips it for a week - and in a regeneration pass it would miss
            # exactly what the pass exists to deliver. The next pass retries
            # these, and only these.
            marks = ",".join("?" for _ in failed)
            with tx() as conn:
                conn.execute(f"UPDATE show SET synced_at = NULL WHERE id IN ({marks})", failed)
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
            "regenerated": regenerate,
        }
        set_meta("last_refresh_at", report["at"])
        set_meta("last_refresh", json.dumps(report))
        if regenerate:
            # Recorded only once the pass is through, so one cut short - the
            # process stopping mid-pass - is retried whole. A show that failed
            # does not hold it up: its synced_at is cleared above instead.
            set_meta("episode_sync_generation", SYNC_GENERATION)
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
