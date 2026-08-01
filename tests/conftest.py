import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402


@pytest.fixture()
def database(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite file for each test."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn
    db.migrate()
    yield db.connect()
    db._local.conn.close()
    del db._local.conn


def make_show(show_id=1, name="Test Show", status="Running", **extra):
    payload = {
        "id": show_id,
        "name": name,
        "status": status,
        "premiered": "2020-01-01",
        "averageRuntime": 45,
        "genres": ["Drama"],
        "schedule": {"time": "21:00", "days": ["Monday"]},
        "externals": {"thetvdb": 99000 + show_id, "imdb": f"tt{show_id:07d}"},
        "image": {"medium": "http://example.com/m.jpg", "original": "http://example.com/o.jpg"},
        "summary": "<p>A <b>show</b>.</p>",
    }
    payload.update(extra)
    return payload


def make_episode(episode_id, season, number, airstamp, show_id=1, **extra):
    payload = {
        "id": episode_id,
        "season": season,
        "number": number,
        "name": f"Episode {number}",
        "type": "regular",
        "airdate": airstamp[:10] if airstamp else None,
        "airstamp": airstamp,
        "runtime": 45,
        "summary": None,
        "image": None,
    }
    payload.update(extra)
    return payload
