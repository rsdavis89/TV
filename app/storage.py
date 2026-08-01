"""Where the database actually is, and whether that place survives a restart.

A container's own filesystem is thrown away every time it restarts. If the
database ends up there — no volume attached, or TV_DATA_DIR pointing at a
relative path inside the app — everything works perfectly until the next
deploy, and then the library is simply gone. The failure is invisible right up
to the moment it costs you everything, so the app now says plainly where it is
writing and warns when that looks temporary.
"""

from __future__ import annotations

from pathlib import Path

from . import config


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _inside(path: Path, base: Path) -> bool:
    try:
        return path.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        return False


def status() -> dict:
    """Describe the storage, flagging anything that will not survive a restart."""
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

    return {
        "database": str(database),
        "exists": database.exists(),
        "size_bytes": size,
        "backups": str(config.BACKUP_DIR),
        "in_container": in_container(),
        "at_risk": at_risk,
        "warning": warning,
    }
