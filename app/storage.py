"""Where the database actually is, and whether that place survives a restart.

A container's own filesystem is thrown away every time it restarts. If the
database ends up there — no volume attached, or TV_DATA_DIR pointing at a
relative path inside the app — everything works perfectly until the next
deploy, and then the library is simply gone. The failure is invisible right up
to the moment it costs you everything, so the app now says plainly where it is
writing and warns when that looks temporary.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config
from .db import utcnow


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _inside(path: Path, base: Path) -> bool:
    try:
        return path.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        return False


def record_start() -> None:
    """Stamp the database's birthday and count how many starts it has seen.

    This is the only honest test of persistence. Configuration can look correct
    and still not survive — a volume attached after the data was written, a
    recreated service, a host that quietly swapped the disk. But a database that
    remembers being created last week, across nine restarts, is demonstrably
    being kept. If those numbers reset after a deploy, the storage is not real,
    whatever the settings say.
    """
    from .db import get_meta, set_meta

    if not get_meta("db_created_at"):
        set_meta("db_created_at", utcnow())
    try:
        starts = int(get_meta("db_starts") or 0)
    except ValueError:
        starts = 0
    set_meta("db_starts", str(starts + 1))


def status() -> dict:
    """Describe the storage, flagging anything that will not survive a restart."""
    from .db import get_meta

    database = config.DB_PATH
    size = database.stat().st_size if database.exists() else 0

    # Inside a container, a database under the application directory is on the
    # container's own disk rather than a mounted volume.
    at_risk = in_container() and _inside(database, config.BASE_DIR)

    warning = None
    if at_risk:
        warning = (
            "This database is on the container's own disk and will be erased the "
            "next time the app restarts. Attach a volume and set TV_DATA_DIR to "
            "its mount path (for example /data)."
        )

    # This report matters most when the database is the thing that is wrong, so
    # never let reading from it be the reason the report fails.
    created_at, starts = None, 0
    try:
        created_at = get_meta("db_created_at")
        starts = int(get_meta("db_starts") or 0)
    except (sqlite3.Error, ValueError, OSError):
        pass

    return {
        "database": str(database),
        "exists": database.exists(),
        "size_bytes": size,
        "created_at": created_at,
        "starts": starts,
        "backups": str(config.BACKUP_DIR),
        "in_container": in_container(),
        "at_risk": at_risk,
        "warning": warning,
    }
