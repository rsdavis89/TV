"""Endpoint-level tests.

These exist because the unit tests call the domain functions directly and so
cannot catch wiring mistakes — a sync endpoint that needs the event loop, a
route that shadows another, a response shape the front end does not expect.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import config, library

from conftest import make_episode, make_show
from test_library import seed


@pytest.fixture()
def client(database, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REFRESH_ON_START", False)
    monkeypatch.setattr(config, "BACKUP_ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")

    async def get_show_with_episodes(show_id):
        return {
            **make_show(show_id=show_id, name=f"Show {show_id}"),
            "_embedded": {
                "episodes": [
                    make_episode(show_id * 100 + 1, 1, 1, "2024-01-01T20:00:00+00:00"),
                    make_episode(show_id * 100 + 2, 1, 2, "2024-01-08T20:00:00+00:00"),
                ]
            },
        }

    monkeypatch.setattr(library.tvmaze, "get_show_with_episodes", get_show_with_episodes)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def wait_for_job(client, job_id, tries=200):
    for _ in range(tries):
        job = client.get(f"/api/import/{job_id}").json()
        if job["status"] != "running":
            return job
        import time

        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_restore_starts_a_job_and_rebuilds_the_library(client, database):
    seed(database)
    library.mark_watched(101)
    library.set_priority(1, True)
    payload = library.export_payload()

    with_db = client.post("/api/restore", json=payload)
    assert with_db.status_code == 200, with_db.text
    job_id = with_db.json()["job_id"]

    job = wait_for_job(client, job_id)
    assert job["status"] == "imported", job.get("error")
    assert job["report"]["format"] == "TV Tracker backup"


def test_restore_rejects_a_file_that_is_not_a_backup(client):
    response = client.post("/api/restore", json={"format": "something-else"})
    assert response.status_code == 400


def test_import_starts_a_job(client, tmp_path):
    export = tmp_path / "history.csv"
    export.write_text("series_name,season,episode,watched_at\nShow 1,1,1,2024-03-03\n")

    response = client.post(
        "/api/import",
        files={"file": ("history.csv", export.read_bytes())},
        data={"dry_run": "true"},
    )
    assert response.status_code == 200
    job = wait_for_job(client, response.json()["job_id"])
    assert job["status"] == "preview"


def test_backup_endpoints(client, database):
    seed(database)
    library.mark_watched(101)

    written = client.post("/api/backups?force=true").json()
    assert written["written"] is True

    listing = client.get("/api/backups").json()
    assert listing["count"] == 1
    name = listing["files"][0]["name"]

    downloaded = client.get(f"/api/backups/{name}")
    assert downloaded.status_code == 200
    assert json.loads(downloaded.content)["format"] == "tv-tracker-export"

    assert client.get("/api/backups/not-a-backup.json").status_code == 404


def test_bulk_endpoint_applies_and_validates(client, database):
    seed(database, show_id=1)
    seed(database, show_id=2, episodes=[make_episode(999, 1, 1, "2024-01-01T00:00:00+00:00", show_id=2)])

    result = client.post("/api/shows/bulk", json={"show_ids": [1, 2], "action": "archive"}).json()
    assert result["changed"] == 2
    assert client.get("/api/shows?filter=archived").json().__len__() == 2

    bad = client.post("/api/shows/bulk", json={"show_ids": [1], "action": "nope"})
    assert bad.status_code == 400


def test_bulk_route_is_not_shadowed_by_the_show_route(client, database):
    """/shows/bulk must not be parsed as /shows/{show_id}."""
    response = client.post("/api/shows/bulk", json={"show_ids": [], "action": "archive"})
    assert response.status_code == 200


def test_priority_endpoint_round_trips(client, database):
    seed(database)
    assert client.post("/api/shows/1/priority", json={"value": True}).json()["card"]["priority"] is True
    assert client.post("/api/shows/1/priority", json={"value": False}).json()["card"]["priority"] is False


def test_home_exposes_every_group_the_front_end_renders(client, database):
    seed(database)
    home = client.get("/api/home").json()
    for group in ("priority", "ready", "not_started", "scheduled", "waiting", "complete"):
        assert group in home, group
        assert group in home["counts"], group


def test_port_falls_back_to_the_platform_variable(monkeypatch):
    """Hosted platforms hand the app a port through PORT; TV_PORT still wins."""
    import importlib

    from app import config as config_module

    def resolved(**env):
        for key in ("TV_PORT", "PORT"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(config_module).PORT

    try:
        assert resolved() == 8484
        assert resolved(PORT="3000") == 3000
        assert resolved(TV_PORT="9000") == 9000
        assert resolved(TV_PORT="9000", PORT="3000") == 9000
        assert resolved(PORT="not-a-number") == 8484
    finally:
        importlib.reload(config_module)


def test_dockerfile_has_no_volume_instruction():
    """Railway and similar hosts reject a Dockerfile that declares VOLUME."""
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    instructions = [
        line.split()[0].upper()
        for line in dockerfile.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "VOLUME" not in instructions
    # And the image must not pin a port, or the platform cannot route to it.
    assert "TV_PORT=" not in dockerfile


def test_status_reports_where_the_database_lives(client):
    """Misplaced storage is invisible until a restart destroys it, so say where."""
    reported = client.get("/api/status").json()["storage"]

    assert reported["database"].endswith(".db")
    assert "backups" in reported
    assert reported["at_risk"] is False
    assert reported["warning"] is None


def test_storage_warns_when_the_database_is_on_container_disk(monkeypatch, tmp_path):
    from app import config as config_module
    from app import storage

    monkeypatch.setattr(storage, "in_container", lambda: True)
    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_module, "DB_PATH", tmp_path / "data" / "tv.db")

    reported = storage.status()
    assert reported["at_risk"] is True
    assert "erased" in reported["warning"]

    # A mounted volume outside the app directory is fine.
    monkeypatch.setattr(config_module, "DB_PATH", tmp_path.parent / "volume" / "tv.db")
    assert storage.status()["at_risk"] is False


def test_storage_counts_restarts_as_proof_of_persistence(database):
    """Correct-looking settings can still lose data; a surviving count cannot."""
    from app import storage

    storage.record_start()
    first = storage.status()
    assert first["starts"] == 1
    assert first["created_at"]

    storage.record_start()
    storage.record_start()
    later = storage.status()

    assert later["starts"] == 3
    # The birthday is stamped once and never moves.
    assert later["created_at"] == first["created_at"]


def test_status_reports_the_app_version_it_serves(client):
    """A stale cached front end makes a good deploy look broken; name the build."""
    from app import storage

    served = client.get("/api/status").json()["storage"]["app_version"]
    assert served != "unknown"
    assert served == storage.served_app_version()


def test_app_js_carries_a_version_matching_what_the_server_reads():
    """The constant in app.js is the one the server extracts."""
    import re
    from pathlib import Path

    from app import storage

    source = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text()
    declared = re.search(r"APP_VERSION\s*=\s*'([^']+)'", source).group(1)
    assert declared == storage.served_app_version()
