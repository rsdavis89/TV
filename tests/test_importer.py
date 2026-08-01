import io
import zipfile

import pytest

from app import importer, library

from conftest import make_episode, make_show


def test_detect_columns_handles_the_tv_time_layout():
    mapping = importer.detect_columns(
        ["episode_id", "tv_show_id", "season_number", "episode_number", "first_watched"]
    )
    assert mapping.show_id == "tv_show_id"
    assert mapping.season == "season_number"
    assert mapping.episode == "episode_number"
    assert mapping.watched == "first_watched"
    assert mapping.has_episode


def test_detect_columns_handles_a_name_only_layout():
    mapping = importer.detect_columns(["Series Name", "Season", "Episode", "Watched At"])
    assert mapping.show_name == "series_name"
    assert mapping.season == "season"
    assert mapping.episode == "episode"
    assert mapping.watched == "watched_at"


def test_detect_columns_never_reads_an_id_as_an_episode_number():
    mapping = importer.detect_columns(["tvdb_id", "episode_id", "created_at"])
    assert mapping.episode is None
    assert not mapping.has_episode
    assert mapping.show_id == "tvdb_id"


def test_detect_columns_reports_no_show_when_there_is_none():
    assert not importer.detect_columns(["season_number", "episode_number"]).has_show


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2024-05-04T21:30:00Z", "2024-05-04T21:30:00+00:00"),
        ("2024-05-04 21:30:00", "2024-05-04T21:30:00+00:00"),
        ("2024-05-04", "2024-05-04T00:00:00+00:00"),
        ("1714857000", "2024-05-04T21:10:00+00:00"),
        ("1714857000000", "2024-05-04T21:10:00+00:00"),
        ("", None),
        ("null", None),
        ("not a date", None),
    ],
)
def test_parse_timestamp(raw, expected):
    assert importer.parse_timestamp(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("S03", 3), ("12", 12), ("Episode 7", 7), ("", None), (None, None), (4, 4)],
)
def test_parse_int(raw, expected):
    assert importer.parse_int(raw) == expected


def test_normalize_title_drops_year_suffix_and_punctuation():
    assert importer.normalize_title("Doctor Who (2005)") == "doctor who"
    assert importer.normalize_title("Marvel's Agents of S.H.I.E.L.D.") == "marvel s agents of s h i e l d"


def build_export(tmp_path):
    """A zip shaped like a TV Time export, including a file we should ignore."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "seen_episode.csv",
            "episode_id,tv_show_id,season_number,episode_number,first_watched\n"
            "5001,99001,1,1,2024-01-02 20:00:00\n"
            "5002,99001,1,2,2024-01-03 20:00:00\n"
            "5003,99001,9,9,2024-01-04 20:00:00\n"
            "5004,42424242,1,1,2024-01-05 20:00:00\n",
        )
        archive.writestr(
            "follows.csv",
            "tv_show_id,tv_show_name,created_at\n99001,Test Show,2023-12-01 10:00:00\n",
        )
        archive.writestr("profile.csv", "username,email\nsomeone,someone@example.com\n")
    path = tmp_path / "tv-time-export.zip"
    path.write_bytes(buffer.getvalue())
    return path


def test_parse_source_classifies_each_file(tmp_path):
    parsed = importer.parse_source(build_export(tmp_path))

    kinds = {item["file"]: item["used"] for item in parsed.files}
    assert kinds["seen_episode.csv"].startswith("episodes")
    assert kinds["follows.csv"].startswith("shows")
    assert kinds["profile.csv"] == "skipped: no show column"

    assert len(parsed.watches) == 4
    assert {key.tvdb_id for key in parsed.follows} == {99001, 42424242}


@pytest.fixture()
def fake_tvmaze(monkeypatch):
    """Stand in for the network: one known show, everything else unknown."""
    show = make_show(show_id=1, name="Test Show")

    async def lookup_by_tvdb(tvdb_id):
        return show if tvdb_id == 99001 else None

    async def search_shows(query):
        return [show] if "test" in query.lower() else []

    async def get_show_with_episodes(show_id):
        return {
            **show,
            "_embedded": {
                "episodes": [
                    make_episode(101, 1, 1, "2024-01-01T20:00:00+00:00"),
                    make_episode(102, 1, 2, "2024-01-08T20:00:00+00:00"),
                ]
            },
        }

    monkeypatch.setattr(importer.tvmaze, "lookup_by_tvdb", lookup_by_tvdb)
    monkeypatch.setattr(importer.tvmaze, "search_shows", search_shows)
    monkeypatch.setattr(library.tvmaze, "get_show_with_episodes", get_show_with_episodes)
    return show


@pytest.mark.asyncio
async def test_dry_run_reports_without_writing(database, fake_tvmaze, tmp_path):
    report = await importer.run_import(build_export(tmp_path), dry_run=True)

    assert report["dry_run"] is True
    assert report["shows_found"] == 1
    assert report["episodes_marked"] == 2
    assert report["episodes_unmatched"] == 1  # the bogus S09E09
    assert report["shows_unmatched"] == ["tvdb 42424242"]

    assert database.execute("SELECT COUNT(*) AS n FROM watch").fetchone()["n"] == 0
    assert database.execute("SELECT COUNT(*) AS n FROM follow").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_commit_writes_watches_and_follows(database, fake_tvmaze, tmp_path):
    report = await importer.run_import(build_export(tmp_path), dry_run=False)

    assert report["episodes_marked"] == 2
    watches = database.execute("SELECT episode_id, watched_at FROM watch ORDER BY episode_id").fetchall()
    assert [row["episode_id"] for row in watches] == [101, 102]
    assert watches[0]["watched_at"] == "2024-01-02T20:00:00+00:00"
    assert database.execute("SELECT COUNT(*) AS n FROM follow").fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_reimport_is_idempotent(database, fake_tvmaze, tmp_path):
    await importer.run_import(build_export(tmp_path), dry_run=False)
    second = await importer.run_import(build_export(tmp_path), dry_run=False)

    assert second["episodes_marked"] == 0
    assert second["episodes_already_known"] == 2
    assert database.execute("SELECT COUNT(*) AS n FROM watch").fetchone()["n"] == 2


@pytest.mark.asyncio
async def test_duplicate_rows_keep_the_earliest_watch_date(database, fake_tvmaze, tmp_path):
    path = tmp_path / "dupes.csv"
    path.write_text(
        "tv_show_id,season_number,episode_number,first_watched\n"
        "99001,1,1,2024-06-01 10:00:00\n"
        "99001,1,1,2024-02-01 10:00:00\n"
    )
    await importer.run_import(path, dry_run=False)

    row = database.execute("SELECT watched_at FROM watch WHERE episode_id = 101").fetchone()
    assert row["watched_at"] == "2024-02-01T10:00:00+00:00"


@pytest.mark.asyncio
async def test_name_only_export_resolves_by_title(database, fake_tvmaze, tmp_path):
    path = tmp_path / "by-name.csv"
    path.write_text(
        "series_name,season,episode,watched_at\nTest Show,1,1,2024-03-03\n"
    )
    report = await importer.run_import(path, dry_run=False)

    assert report["shows_found"] == 1
    assert report["episodes_marked"] == 1
    assert report["shows_guessed"] == []


@pytest.mark.asyncio
async def test_import_records_a_job(database, fake_tvmaze, tmp_path):
    await importer.run_import(build_export(tmp_path), dry_run=True, filename="export.zip")
    row = database.execute("SELECT filename, status FROM import_job").fetchone()

    assert row["filename"] == "export.zip"
    assert row["status"] == "preview"
