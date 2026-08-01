"""Runtime configuration, all overridable by environment variables."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    return _first_env_int((name,), default)


def _first_env_int(names: tuple[str, ...], default: int) -> int:
    """First of these variables that holds a number wins."""
    for name in names:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return default


# Where the SQLite database lives. Keep this on a volume you back up.
DATA_DIR = Path(os.environ.get("TV_DATA_DIR") or (BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("TV_DB_PATH") or (DATA_DIR / "tv.db"))

# Optional shared password. When unset the app is wide open, which is fine on a
# home network but not on the public internet.
PASSWORD = os.environ.get("TV_PASSWORD") or ""
SECRET_KEY = os.environ.get("TV_SECRET_KEY") or ""
SESSION_DAYS = _env_int("TV_SESSION_DAYS", 90)

# How often to look for new episodes of followed shows.
REFRESH_INTERVAL_HOURS = _env_int("TV_REFRESH_INTERVAL_HOURS", 6)
REFRESH_ON_START = _env_bool("TV_REFRESH_ON_START", True)

# A show that has not been touched in this long gets re-synced even if the
# TVmaze updates feed did not flag it.
STALE_SHOW_HOURS = _env_int("TV_STALE_SHOW_HOURS", 24 * 7)

# Automatic backups of the watch history. This is the only data in the app that
# cannot be fetched again from anywhere, so it is on by default.
BACKUP_ENABLED = _env_bool("TV_BACKUP_ENABLED", True)
BACKUP_DIR = Path(os.environ.get("TV_BACKUP_DIR") or (DATA_DIR / "backups"))
BACKUP_INTERVAL_HOURS = _env_int("TV_BACKUP_INTERVAL_HOURS", 24)
BACKUP_KEEP = _env_int("TV_BACKUP_KEEP", 14)

TVMAZE_BASE = os.environ.get("TV_TVMAZE_BASE", "https://api.tvmaze.com")
# TVmaze asks for at most 20 calls per 10 seconds. Stay under it.
TVMAZE_RATE = _env_int("TV_TVMAZE_RATE", 18)
TVMAZE_RATE_WINDOW = _env_int("TV_TVMAZE_RATE_WINDOW", 10)

HOST = os.environ.get("TV_HOST", "0.0.0.0")
# Hosted platforms hand the app a port through PORT and route traffic to it.
# TV_PORT wins when set, so an explicit choice still beats the platform's.
PORT = _first_env_int(("TV_PORT", "PORT"), 8484)
