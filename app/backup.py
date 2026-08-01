"""Automatic backups of the watch history.

The library is the one thing here that cannot be re-fetched: show and episode
data comes back from TVmaze any time, but a decade of "I watched this" is
irreplaceable, and TV Time is gone. So the app writes a dated JSON snapshot on a
schedule and keeps the last N of them, rather than relying on anyone remembering
to press a button.

Snapshots are plain JSON in the same format as the manual download, so restoring
needs nothing but the file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from . import config, library
from .db import get_meta, set_meta, utcnow

log = logging.getLogger("tv.backup")

PREFIX = "tv-backup-"
SUFFIX = ".json"


def backup_dir() -> Path:
    path = config.BACKUP_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fingerprint(payload: dict) -> str:
    """Hash the contents, ignoring the timestamp that changes every run."""
    body = {"follows": payload.get("follows"), "watches": payload.get("watches")}
    return sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def list_backups() -> list[dict]:
    """Newest first."""
    if not config.BACKUP_DIR.exists():
        return []
    files = [
        path
        for path in config.BACKUP_DIR.iterdir()
        if path.name.startswith(PREFIX) and path.name.endswith(SUFFIX) and path.is_file()
    ]
    # By write time rather than name: two snapshots in the same second get a
    # disambiguating suffix, which does not sort the way the clock does.
    files.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return [
        {
            "name": path.name,
            "bytes": path.stat().st_size,
            "modified": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        }
        for path in files
    ]


def prune(keep: int) -> list[str]:
    removed = []
    for item in list_backups()[max(keep, 1) :]:
        try:
            (config.BACKUP_DIR / item["name"]).unlink()
            removed.append(item["name"])
        except OSError as exc:
            log.warning("could not remove old backup %s: %s", item["name"], exc)
    return removed


def write_backup(force: bool = False) -> dict:
    """Write a snapshot unless nothing has changed since the last one."""
    payload = library.export_payload()
    fingerprint = _fingerprint(payload)

    if not force and fingerprint == get_meta("last_backup_fingerprint"):
        # Nothing watched since the last snapshot; another identical copy would
        # only push a real one out of the retention window.
        set_meta("last_backup_check", utcnow())
        return {
            "written": False,
            "reason": "no changes since the last backup",
            "at": get_meta("last_backup_at"),
            "backups": len(list_backups()),
        }

    directory = backup_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    target = directory / f"{PREFIX}{stamp}{SUFFIX}"
    # Two snapshots inside the same second must not overwrite each other.
    counter = 2
    while target.exists():
        target = directory / f"{PREFIX}{stamp}-{counter}{SUFFIX}"
        counter += 1
    temporary = target.with_suffix(".tmp")

    # Write then rename, so a crash mid-write cannot leave a half-file that
    # looks like a valid backup.
    temporary.write_text(json.dumps(payload, indent=2))
    os.replace(temporary, target)

    removed = prune(config.BACKUP_KEEP)
    set_meta("last_backup_at", utcnow())
    set_meta("last_backup_check", utcnow())
    set_meta("last_backup_fingerprint", fingerprint)

    log.info(
        "wrote %s (%d follows, %d watches)",
        target.name,
        len(payload["follows"]),
        len(payload["watches"]),
    )
    return {
        "written": True,
        "name": target.name,
        "bytes": target.stat().st_size,
        "at": get_meta("last_backup_at"),
        "follows": len(payload["follows"]),
        "watches": len(payload["watches"]),
        "pruned": removed,
        "backups": len(list_backups()),
    }


def status() -> dict:
    backups = list_backups()
    return {
        "enabled": config.BACKUP_ENABLED,
        "directory": str(config.BACKUP_DIR),
        "interval_hours": config.BACKUP_INTERVAL_HOURS,
        "keep": config.BACKUP_KEEP,
        "last_backup_at": get_meta("last_backup_at"),
        "last_checked_at": get_meta("last_backup_check"),
        "count": len(backups),
        "latest": backups[0] if backups else None,
    }


async def scheduler() -> None:
    """Back up on startup, then on the configured interval."""
    if not config.BACKUP_ENABLED:
        log.info("automatic backups are disabled")
        return
    interval = max(config.BACKUP_INTERVAL_HOURS, 1) * 3600
    while True:
        try:
            await asyncio.to_thread(write_backup)
        except Exception:  # a failed backup must never take the app down
            log.exception("backup failed")
        await asyncio.sleep(interval)
