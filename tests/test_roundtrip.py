"""Moving to another machine means: back up here, restore there, nothing lost.

These tests take a full library, export it, wipe the database, restore from the
backup, and compare. Anything the backup does not carry shows up as a diff.
"""

import json

import pytest

from app import backup, config, db, importer, library

from conftest import make_episode, make_show


def build_library(database):
    """A library exercising every flag and shape that has to survive a move."""
    for show_id, name in [(1, "Watched Show"), (2, "Archived Show"), (3, "Pinned Show")]:
        library.save_show(make_show(show_id=show_id, name=name))
        library.save_episodes(
            show_id,
            [
                make_episode(show_id * 100 + 1, 1, 1, "2024-01-01T20:00:00+00:00"),
                make_episode(show_id * 100 + 2, 1, 2, "2024-01-08T20:00:00+00:00"),
                make_episode(show_id * 100 + 3, 1, 3, "2024-01-15T20:00:00+00:00"),
            ],
        )
        library.follow_show(show_id)

    library.mark_watched(101, watched_at="2024-02-01T10:00:00+00:00")
    library.mark_watched(103, watched_at="2024-02-05T10:00:00+00:00")  # a gap at 102
    library.set_favorite(1, True)
    library.set_archived(2, True)
    library.mark_watched(201, watched_at="2023-06-06T06:00:00+00:00")
    library.set_priority(3, True)
    library.set_favorite(3, True)


def snapshot() -> dict:
    """Everything a move has to preserve, in a comparable shape."""
    conn = db.connect()
    return {
        "follows": [
            dict(row)
            for row in conn.execute(
                "SELECT show_id, archived, favorite, priority FROM follow ORDER BY show_id"
            )
        ],
        "watches": [
            dict(row)
            for row in conn.execute(
                "SELECT episode_id, show_id, watched_at FROM watch ORDER BY episode_id"
            )
        ],
    }


@pytest.fixture()
def offline_tvmaze(monkeypatch):
    """The new machine re-fetches show data; serve it locally instead."""

    async def get_show_with_episodes(show_id):
        return {
            **make_show(show_id=show_id, name=f"Show {show_id}"),
            "_embedded": {
                "episodes": [
                    make_episode(show_id * 100 + 1, 1, 1, "2024-01-01T20:00:00+00:00"),
                    make_episode(show_id * 100 + 2, 1, 2, "2024-01-08T20:00:00+00:00"),
                    make_episode(show_id * 100 + 3, 1, 3, "2024-01-15T20:00:00+00:00"),
                ]
            },
        }

    monkeypatch.setattr(library.tvmaze, "get_show_with_episodes", get_show_with_episodes)


@pytest.mark.asyncio
async def test_backup_restores_onto_an_empty_database(database, offline_tvmaze, tmp_path):
    build_library(database)
    before = snapshot()

    payload = library.export_payload()
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(payload))

    # Wipe everything, as if this were a brand new machine.
    with db.tx() as conn:
        conn.execute("DELETE FROM watch")
        conn.execute("DELETE FROM follow")
        conn.execute("DELETE FROM episode")
        conn.execute("DELETE FROM show")
    assert snapshot() == {"follows": [], "watches": []}

    await importer.run_import(path, dry_run=False)

    assert snapshot() == before


@pytest.mark.asyncio
async def test_restore_keeps_priority_and_favorite_flags(database, offline_tvmaze, tmp_path):
    build_library(database)
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(library.export_payload()))

    with db.tx() as conn:
        conn.execute("DELETE FROM watch")
        conn.execute("DELETE FROM follow")
    await importer.run_import(path, dry_run=False)

    assert library.show_card(3)["priority"] is True
    assert library.show_card(3)["favorite"] is True
    assert library.show_card(1)["favorite"] is True
    assert library.show_card(1)["priority"] is False
    assert library.show_card(2)["archived"] is True


@pytest.mark.asyncio
async def test_restore_preserves_watch_dates_and_gaps(database, offline_tvmaze, tmp_path):
    build_library(database)
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(library.export_payload()))

    with db.tx() as conn:
        conn.execute("DELETE FROM watch")
        conn.execute("DELETE FROM follow")
    await importer.run_import(path, dry_run=False)

    row = database.execute("SELECT watched_at FROM watch WHERE episode_id = 101").fetchone()
    assert row["watched_at"] == "2024-02-01T10:00:00+00:00"
    # The deliberate gap at 102 is still a gap, so up-next is unchanged.
    assert library.next_episode(1)["id"] == 102
    assert library.gaps_before_furthest(1) == 1


@pytest.mark.asyncio
async def test_restoring_over_an_existing_library_is_idempotent(
    database, offline_tvmaze, tmp_path
):
    build_library(database)
    before = snapshot()
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(library.export_payload()))

    report = await importer.run_import(path, dry_run=False)

    assert report["episodes_marked"] == 0
    assert report["episodes_already_known"] == 3
    assert snapshot() == before


@pytest.mark.asyncio
async def test_a_written_backup_file_restores(database, offline_tvmaze, tmp_path, monkeypatch):
    """The real file the scheduler writes, not just an in-memory payload."""
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    build_library(database)
    before = snapshot()

    result = backup.write_backup()
    path = tmp_path / "backups" / result["name"]

    with db.tx() as conn:
        conn.execute("DELETE FROM watch")
        conn.execute("DELETE FROM follow")
        conn.execute("DELETE FROM episode")
        conn.execute("DELETE FROM show")

    await importer.run_import(path, dry_run=False)
    assert snapshot() == before


def test_backup_is_recognised_as_its_own_format(database, tmp_path):
    build_library(database)
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(library.export_payload()))

    parsed = importer.parse_source(path)
    assert parsed.format == "TV Tracker backup"
    # Shows carry their TVmaze id, so restoring needs no lookups at all.
    assert all(record.key.tvmaze_id for record in parsed.shows.values())


def test_a_non_backup_json_is_not_mistaken_for_one(tmp_path):
    path = tmp_path / "other.json"
    path.write_text(json.dumps([{"series_name": "Thing", "season": 1, "episode": 1}]))
    assert importer.read_own_export(path) is None
