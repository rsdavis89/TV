"""Endpoint-level tests.

These exist because the unit tests call the domain functions directly and so
cannot catch wiring mistakes — a sync endpoint that needs the event loop, a
route that shadows another, a response shape the front end does not expect.
"""

import json
from pathlib import Path

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
    for group in ("ready", "not_started", "scheduled", "waiting", "complete"):
        assert group in home, group
        assert group in home["counts"], group
    # Priority is its own tab now, not a section here.
    assert "priority" not in home
    assert "priority_waiting" in home["counts"]


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


def test_storage_warns_when_the_database_is_inside_the_app_folder(monkeypatch, tmp_path):
    from app import config as config_module
    from app import storage

    monkeypatch.setattr(storage, "in_container", lambda: True)
    monkeypatch.setattr(storage, "on_root_filesystem", lambda path: True)
    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_module, "DB_PATH", tmp_path / "data" / "tv.db")

    reported = storage.status()
    assert reported["at_risk"] is True
    assert "application folder" in reported["warning"]


def test_storage_warns_when_the_data_dir_is_not_a_mounted_volume(monkeypatch, tmp_path):
    """The failure that cost a library: TV_DATA_DIR=/data with nothing mounted.

    The path is spelled perfectly and sits outside the app, so only the
    filesystem check can tell it is an ordinary folder in the image.
    """
    from app import config as config_module
    from app import storage

    monkeypatch.setattr(storage, "in_container", lambda: True)
    monkeypatch.setattr(storage, "on_root_filesystem", lambda path: True)
    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path / "app")
    monkeypatch.setattr(config_module, "DB_PATH", Path("/data/tv.db"))

    reported = storage.status()
    assert reported["at_risk"] is True
    assert "no volume is mounted" in reported["warning"]
    assert "/data" in reported["warning"]


def test_storage_is_content_when_the_data_dir_is_a_real_mount(monkeypatch, tmp_path):
    from app import config as config_module
    from app import storage

    monkeypatch.setattr(storage, "in_container", lambda: True)
    monkeypatch.setattr(storage, "on_root_filesystem", lambda path: False)
    monkeypatch.setattr(config_module, "DB_PATH", Path("/data/tv.db"))

    reported = storage.status()
    assert reported["at_risk"] is False
    assert reported["warning"] is None


def test_root_filesystem_check_distinguishes_a_real_mount():
    """A mounted filesystem reports a different device id from /."""
    import os

    from app import storage

    assert storage.on_root_filesystem(Path("/etc/hostname")) is True
    if Path("/dev/shm").exists() and os.stat("/dev/shm").st_dev != os.stat("/").st_dev:
        assert storage.on_root_filesystem(Path("/dev/shm/anything")) is False


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


def test_status_exposes_the_inputs_behind_its_verdict(client):
    """The UI states 'mounted volume' or 'container disk' from these two."""
    reported = client.get("/api/status").json()["storage"]

    assert "in_container" in reported
    assert reported["on_container_disk"] in (True, False, None)


def test_a_show_you_have_not_added_can_still_be_opened(client, database):
    """Tapping a search result should show the show, not a 404."""
    response = client.get("/api/shows/4242")
    assert response.status_code == 200, response.text

    detail = response.json()
    assert detail["following"] is False
    assert detail["seasons"], "episodes should be listed for a preview"

    # Previewing does not follow it.
    assert client.get("/api/shows?filter=active").json() == []


def test_opening_an_unknown_show_still_404s(client, monkeypatch):
    from app import library, tvmaze

    async def missing(show_id):
        raise tvmaze.NotFound("nope")

    monkeypatch.setattr(library.tvmaze, "get_show_with_episodes", missing)
    assert client.get("/api/shows/999999").status_code == 404


def test_premieres_endpoint_shape(client):
    body = client.get("/api/premieres").json()

    for key in ("premieres", "channels", "defaults", "groups", "window"):
        assert key in body, key
    # The default services are the ones the UI ticks on first run.
    assert "Netflix" in body["defaults"]
    assert "Prime Video" in body["defaults"]


# --------------------------------------------------------------------------
# the password gate
# --------------------------------------------------------------------------


@pytest.fixture()
def locked(client, monkeypatch):
    """The same app with the password gate switched on."""
    from app import auth, config as app_config

    monkeypatch.setattr(app_config, "PASSWORD", "correct horse")
    # The throttle really does sleep. Keep the shape, drop the wall-clock cost.
    monkeypatch.setattr(auth, "MAX_DELAY_SECONDS", 0.01)
    auth.clear_failures()
    yield client
    auth.clear_failures()


def test_the_api_is_closed_until_you_log_in(locked):
    assert locked.get("/api/home").status_code == 401
    # The schema is part of the API, not a public page.
    assert locked.get("/api/openapi.json").status_code == 401
    # But the login route itself has to stay reachable.
    assert locked.get("/api/auth/status").status_code == 200


def test_a_wrong_password_is_rejected_and_a_right_one_lets_you_in(locked):
    assert locked.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert locked.get("/api/home").status_code == 401

    assert locked.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    assert locked.get("/api/home").status_code == 200


def test_wrong_guesses_make_the_next_one_slower(database):
    """Checked against the real constants, without paying the delay."""
    from app import auth

    auth.clear_failures()
    try:
        # A couple of typos should not cost anything.
        for _ in range(auth.FREE_ATTEMPTS):
            auth.record_failure()
        assert auth.failure_delay() == 0

        seen = []
        for _ in range(6):
            auth.record_failure()
            seen.append(auth.failure_delay())

        assert seen == sorted(seen), seen
        assert seen[0] == 1
        assert seen[-1] == auth.MAX_DELAY_SECONDS
    finally:
        auth.clear_failures()


def test_the_throttle_is_wired_into_the_endpoint(locked):
    from app import auth

    for _ in range(auth.FREE_ATTEMPTS + 3):
        locked.post("/api/auth/login", json={"password": "nope"})
    assert auth.failure_delay() > 0


def test_a_flood_of_guesses_is_refused_outright(locked):
    from app import auth

    for _ in range(auth.MAX_FAILURES):
        auth.record_failure()

    blocked = locked.post("/api/auth/login", json={"password": "nope"})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers

    # And the refusal is not itself a way past the gate.
    assert locked.get("/api/home").status_code == 401


def test_the_right_password_still_works_while_guesses_are_being_slowed(locked):
    """A lockout would let an attacker keep the owner out; a delay must not."""
    from app import auth

    for _ in range(auth.FREE_ATTEMPTS + 3):
        locked.post("/api/auth/login", json={"password": "nope"})
    assert auth.failure_delay() > 0

    assert locked.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    assert auth.failure_delay() == 0
    assert locked.get("/api/home").status_code == 200


def test_the_session_cookie_is_not_readable_by_scripts(locked):
    response = locked.post("/api/auth/login", json={"password": "correct horse"})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


def test_a_forged_session_cookie_is_refused(locked):
    import time

    from app import auth

    future = str(int(time.time()) + 86400)
    for forged in (f"{future}.deadbeef", future, "", f"{future}."):
        assert auth.valid_token(forged) is False


def test_an_expired_but_correctly_signed_token_is_refused(database):
    import time

    from app import auth

    past = str(int(time.time()) - 10)
    assert auth.valid_token(f"{past}.{auth._sign(past)}") is False


def test_oversized_uploads_are_refused(client, monkeypatch):
    from app import api

    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 1024)
    response = client.post(
        "/api/import",
        files={"file": ("big.zip", b"x" * 4096, "application/zip")},
        data={"dry_run": "true"},
    )
    assert response.status_code == 413


def test_opening_a_show_stored_before_cast_was_kept_fetches_it(client, database, monkeypatch):
    """The upgrade path: hundreds of shows already stored, none with a cast."""
    from app import library

    seed(database)
    assert library.show_public(library._show_row(1))["cast"] is None

    calls = []

    async def get_cast(show_id):
        calls.append(show_id)
        return [{
            "person": {"id": 9, "name": "Ada", "image": {"medium": "http://x/p.jpg"}},
            "character": {"id": 90, "name": "Captain"},
        }]

    monkeypatch.setattr(library.tvmaze, "get_cast", get_cast)

    body = client.get("/api/shows/1").json()
    assert [m["name"] for m in body["show"]["cast"]] == ["Ada"]

    # Stored, so opening it again does not go back out to the network.
    client.get("/api/shows/1")
    assert calls == [1]


def test_a_show_with_no_cast_on_record_is_not_asked_for_twice(client, database, monkeypatch):
    from app import library

    seed(database)
    calls = []

    async def get_cast(show_id):
        calls.append(show_id)
        return []

    monkeypatch.setattr(library.tvmaze, "get_cast", get_cast)

    assert client.get("/api/shows/1").json()["show"]["cast"] == []
    client.get("/api/shows/1")
    assert calls == [1]


def make_dated_show(client, database, show_id, name, premiered, first_episode):
    from conftest import make_episode, make_show

    library.save_show({**make_show(show_id=show_id, name=name), "premiered": premiered})
    library.save_episodes(show_id, [
        make_episode(show_id * 100 + 1, 1, 1, f"{first_episode}T20:00:00+00:00", show_id=show_id)
    ])
    library.follow_show(show_id)


def test_shows_can_be_sorted_by_when_they_first_aired(client, database):
    make_dated_show(client, database, 1, "Middle", "2010-06-01", "2010-06-01")
    make_dated_show(client, database, 2, "Oldest", "1999-01-04", "1999-01-04")
    make_dated_show(client, database, 3, "Newest", "2024-11-20", "2024-11-20")

    newest = client.get("/api/shows?sort=newest").json()
    assert [c["show"]["name"] for c in newest] == ["Newest", "Middle", "Oldest"]

    oldest = client.get("/api/shows?sort=oldest").json()
    assert [c["show"]["name"] for c in oldest] == ["Oldest", "Middle", "Newest"]


def test_a_show_without_a_premiere_date_falls_back_to_its_first_episode(client, database):
    """TVmaze leaves `premiered` empty sometimes; the episodes still know."""
    make_dated_show(client, database, 1, "Dated", "2015-03-01", "2015-03-01")
    make_dated_show(client, database, 2, "Undated", None, "1990-05-05")

    ordered = client.get("/api/shows?sort=oldest").json()
    assert [c["show"]["name"] for c in ordered] == ["Undated", "Dated"]
    assert ordered[0]["first_aired"] == "1990-05-05"


def test_the_show_record_wins_over_a_late_starting_episode_list(client, database):
    """The episode list is not always complete at the front.

    TVmaze has 8 Out of 10 Cats Does Countdown premiering in January 2012 and
    lists no episode before April 2013, so the record is the better answer.
    """
    make_dated_show(client, database, 1, "Late list", "2012-01-02", "2013-04-12")

    [card] = client.get("/api/shows?sort=oldest").json()
    assert card["first_aired"] == "2012-01-02"


def test_shows_with_no_date_at_all_sort_last_in_both_directions(client, database):
    from conftest import make_show

    make_dated_show(client, database, 1, "Dated", "2015-03-01", "2015-03-01")
    library.save_show({**make_show(show_id=2, name="Nothing known"), "premiered": None})
    library.follow_show(2)

    for order in ("newest", "oldest"):
        names = [c["show"]["name"] for c in client.get(f"/api/shows?sort={order}").json()]
        assert names[-1] == "Nothing known", order


def test_clearing_finished_priority_shows_leaves_the_rest_alone(client, database):
    """What the Priority tab's Clear finished button does, end to end."""
    from conftest import make_episode, make_show

    def pinned(show_id, name, status, airstamp, watched):
        library.save_show({**make_show(show_id=show_id, name=name), "status": status})
        library.save_episodes(show_id, [
            make_episode(show_id * 100 + 1, 1, 1, airstamp, show_id=show_id)
        ])
        library.follow_show(show_id)
        library.set_priority(show_id, True)
        if watched:
            library.mark_watched(show_id * 100 + 1)

    # Ended and fully watched: finished.
    pinned(1, "Done", "Ended", "2020-01-01T20:00:00+00:00", watched=True)
    # Running and fully watched: caught up, but it may come back.
    pinned(2, "Between seasons", "Running", "2020-01-01T20:00:00+00:00", watched=True)
    # Ended but not finished: still something to watch.
    pinned(3, "Half done", "Ended", "2020-01-01T20:00:00+00:00", watched=False)

    before = {c["show"]["name"]: c["status"] for c in client.get("/api/shows?filter=priority").json()}
    assert before == {"Done": "complete", "Between seasons": "caught_up", "Half done": "ready"}

    result = client.post(
        "/api/shows/bulk", json={"show_ids": [1], "action": "unpriority"}
    ).json()
    assert result["changed"] == 1
    assert result["verb"] == "unpinned"

    after = sorted(c["show"]["name"] for c in client.get("/api/shows?filter=priority").json())
    assert after == ["Between seasons", "Half done"]

    # Unpinning is not unfollowing, and it does not touch watch history.
    assert sorted(c["show"]["name"] for c in client.get("/api/shows").json()) == [
        "Between seasons", "Done", "Half done"
    ]
    assert client.get("/api/shows/1").json()["progress"]["watched"] == 1


def test_the_watch_log_is_newest_first_and_pages(client, database):
    """Backs the Watch Log screen: what you watched, in the order you watched it."""
    seed(database)
    library.mark_watched(101, watched_at="2024-03-01T20:00:00+00:00")
    library.mark_watched(102, watched_at="2024-03-03T21:30:00+00:00")

    log = client.get("/api/history").json()
    assert [row["watched_at"] for row in log] == [
        "2024-03-03T21:30:00+00:00", "2024-03-01T20:00:00+00:00"
    ]
    # Everything the screen renders per row.
    for key in ("show_id", "show_name", "show_image", "code", "watched_at", "source"):
        assert key in log[0], key

    assert len(client.get("/api/history?limit=1").json()) == 1
    assert client.get("/api/history?limit=1&offset=1").json()[0]["watched_at"] \
        == "2024-03-01T20:00:00+00:00"
    assert client.get("/api/history?limit=1&offset=2").json() == []


def test_the_watch_log_records_where_a_mark_came_from(client, database):
    """Imported history and episodes ticked in the app read differently."""
    seed(database)
    library.mark_watched(101)
    library.mark_watched(102, source="import")

    sources = {row["id"]: row["source"] for row in client.get("/api/history").json()}
    assert sources == {101: "app", 102: "import"}
