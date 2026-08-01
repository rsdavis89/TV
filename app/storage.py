"""Where the database actually is, and whether that place survives a restart.

A container's own filesystem is thrown away every time it restarts. If the
database ends up there — no volume attached, or TV_DATA_DIR pointing at a
relative path inside the app — everything works perfectly until the next
deploy, and then the library is simply gone. The failure is invisible right up
to the moment it costs you everything, so the app now says plainly where it is
writing and warns when that looks temporary.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from . import config
from .db import utcnow


def served_app_version() -> str:
    """The APP_VERSION constant in the app.js this server would hand out."""
    try:
        text = (config.WEB_DIR / "app.js").read_text()
    except OSError:
        return "unknown"
    match = re.search(r"APP_VERSION\s*=\s*'([^']+)'", text)
    return match.group(1) if match else "unknown"


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _inside(path: Path, base: Path) -> bool:
    try:
        return path.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        return False


def _nearest_existing(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path("/")


def on_root_filesystem(path: Path) -> bool | None:
    """Is this path on the container image's own filesystem?

    A mounted volume is a different filesystem, so it reports a different device
    id from `/`. Matching `/` means the file is in the container itself and dies
    with it. This catches the case a path check cannot: TV_DATA_DIR set to
    /data, spelled perfectly, with no volume actually mounted there — the
    directory is then just an ordinary folder inside the image.
    """
    try:
        return os.stat(_nearest_existing(path)).st_dev == os.stat("/").st_dev
    except OSError:
        return None


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

    # Inside a container, anything sharing a filesystem with / belongs to the
    # image and does not outlive it. This catches a correctly spelled
    # TV_DATA_DIR with no volume actually mounted there, which a path check
    # cannot see.
    at_risk = in_container() and on_root_filesystem(database) is True

    warning = None
    if at_risk and _inside(database, config.BASE_DIR):
        warning = (
            f"The database is inside the application folder ({database}), on the "
            "container's own disk, so it will be erased the next time the app "
            "restarts. Set TV_DATA_DIR to your volume's mount path, e.g. /data."
        )
    elif at_risk:
        warning = (
            f"TV_DATA_DIR points at {database.parent}, but no volume is mounted "
            "there — it is an ordinary folder inside the container, so it will be "
            "erased the next time the app restarts. Check that a volume is "
            f"attached to this service with its mount path set to exactly "
            f"{database.parent}."
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
        "app_version": served_app_version(),
        "backups": str(config.BACKUP_DIR),
        "in_container": in_container(),
        "on_container_disk": on_root_filesystem(database),
        "at_risk": at_risk,
        "warning": warning,
    }
