import json

import pytest

from app import backup, config, library

from conftest import make_episode
from test_library import seed


@pytest.fixture()
def backups(tmp_path, monkeypatch, database):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "BACKUP_KEEP", 3)
    return tmp_path / "backups"


def test_backup_writes_a_readable_snapshot(backups, database):
    show_id = seed(database)
    library.mark_watched(101)
    library.set_favorite(show_id, True)

    result = backup.write_backup()
    assert result["written"] is True
    assert result["watches"] == 1

    payload = json.loads((backups / result["name"]).read_text())
    assert payload["format"] == "tv-tracker-export"
    assert payload["watches"][0]["season"] == 1
    assert payload["follows"][0]["favorite"] == 1


def test_backup_skips_when_nothing_changed(backups, database):
    seed(database)
    library.mark_watched(101)

    first = backup.write_backup()
    second = backup.write_backup()

    assert first["written"] is True
    assert second["written"] is False
    assert "no changes" in second["reason"]
    assert len(backup.list_backups()) == 1


def test_backup_writes_again_once_something_changes(backups, database):
    seed(database)
    library.mark_watched(101)
    backup.write_backup()

    library.mark_watched(102)
    assert backup.write_backup()["written"] is True
    assert len(backup.list_backups()) == 2


def test_force_writes_even_with_no_changes(backups, database):
    seed(database)
    library.mark_watched(101)
    backup.write_backup()

    assert backup.write_backup(force=True)["written"] is True
    assert len(backup.list_backups()) == 2


def test_old_backups_are_pruned_newest_kept(backups, database):
    seed(database)
    for episode_id in (101, 102, 103, 104):
        library.mark_watched(episode_id)
        backup.write_backup()

    listed = backup.list_backups()
    assert len(listed) == config.BACKUP_KEEP
    # Newest first, and the oldest snapshots are the ones that were dropped.
    times = [item["modified"] for item in listed]
    assert times == sorted(times, reverse=True)
    newest = json.loads((backups / listed[0]["name"]).read_text())
    assert len(newest["watches"]) == 4


def test_backup_leaves_no_partial_files(backups, database):
    seed(database)
    library.mark_watched(101)
    backup.write_backup()

    assert not list(backups.glob("*.tmp"))
    assert all(path.suffix == ".json" for path in backups.iterdir())


def test_status_reports_the_latest_backup(backups, database):
    seed(database)
    library.mark_watched(101)
    backup.write_backup()

    status = backup.status()
    assert status["count"] == 1
    assert status["last_backup_at"]
    assert status["latest"]["name"].startswith("tv-backup-")


def test_backup_round_trips_through_export_payload(backups, database):
    show_id = seed(database)
    library.mark_through(show_id, 103)
    library.set_priority(show_id, True)

    result = backup.write_backup()
    payload = json.loads((backups / result["name"]).read_text())

    assert len(payload["watches"]) == 3
    assert payload["follows"][0]["priority"] == 1
