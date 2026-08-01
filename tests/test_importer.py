import io
import zipfile

import pytest

from app import importer, library

from conftest import make_episode, make_show


# --------------------------------------------------------------------------
# generic column sniffing
# --------------------------------------------------------------------------


def test_detect_columns_handles_a_name_only_layout():
    mapping = importer.detect_columns(["Series Name", "Season", "Episode", "Watched At"])
    assert mapping.show_name == "series_name"
    assert mapping.season == "season"
    assert mapping.episode == "episode"
    assert mapping.watched == "watched_at"


def test_detect_columns_finds_tvdb_ids_under_several_names():
    for header, expected in [
        ("tv_show_id", "tv_show_id"),
        ("tvdb_id", "tvdb_id"),
        ("s_id", "s_id"),
        ("series_id", "series_id"),
    ]:
        mapping = importer.detect_columns([header, "season_number", "episode_number"])
        assert mapping.show_id == expected


def test_detect_columns_never_reads_an_id_as_an_episode_number():
    mapping = importer.detect_columns(["tvdb_id", "episode_id", "created_at"])
    assert mapping.episode is None
    assert not mapping.has_episode
    assert mapping.show_id == "tvdb_id"


def test_detect_columns_reports_no_show_when_there_is_none():
    assert not importer.detect_columns(["season_number", "episode_number"]).has_show


def test_generic_column_names_are_not_fuzzy_matched():
    """'region_name' is not a show, and 'version_number' is not an episode."""
    mapping = importer.detect_columns(["region_name", "city_name", "created_at"])
    assert mapping.show_name is None

    mapping = importer.detect_columns(["name", "version_number", "created_at"])
    assert mapping.episode is None


def test_detect_columns_picks_up_follow_flags():
    mapping = importer.detect_columns(["tv_show_id", "tv_show_name", "active", "archived"])
    assert mapping.followed == "active"
    assert mapping.archived == "archived"


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


@pytest.mark.parametrize(
    "raw,expected",
    [("1", True), ("true", True), ("0", False), ("false", False), ("", None), (None, None)],
)
def test_parse_bool(raw, expected):
    assert importer.parse_bool(raw) == expected


def test_normalize_title_drops_year_suffix_and_punctuation():
    assert importer.normalize_title("Doctor Who (2005)") == "doctor who"
    assert (
        importer.normalize_title("Marvel's Agents of S.H.I.E.L.D.")
        == "marvel s agents of s h i e l d"
    )


def test_show_key_identity_prefers_tvdb_id():
    a = importer.ShowKey(tvdb_id=1234, name="Buffy the Vampire Slayer")
    b = importer.ShowKey(tvdb_id=1234, name="Buffy The Vampire Slayer")
    c = importer.ShowKey(tvdb_id=None, name="Buffy the Vampire Slayer (1997)")
    d = importer.ShowKey(tvdb_id=None, name="buffy the vampire slayer")

    assert a.identity() == b.identity()
    assert c.identity() == d.identity()
    assert a.identity() != c.identity()


# --------------------------------------------------------------------------
# the real TV Time export layout
# --------------------------------------------------------------------------

# Headers copied from an actual TV Time GDPR export; the rows are invented.
V2_HEADER = (
    "user_id,created_at,s_id,ep_id,gsi,key,ep_watch_count,series_follow_count,"
    "total_movies_runtime,movie_watch_count,updated_at,total_series_runtime,uuid,"
    "is_for_later,is_archived,is_followed,most_recent_ep_watched,followed_at,ep_no,"
    "rewatch_count,is_unitary,s_no,runtime,bulk_type,movie_name,series_name,"
    "season_number,episode_number\n"
)


def v2_watch(s_id, name, season, episode, created_at, key="watch-episode-1"):
    return (
        f"1,{created_at},{s_id},9,watch-episode-1,{key},,,,,,,,,,,,,,,,,,,,"
        f"{name},{season},{episode}\n"
    )


def v2_series(s_id, name, followed, archived):
    return (
        f"1,2020-01-01 00:00:00,{s_id},,,user-series-{s_id},,,,,,,,,"
        f"{archived},{followed},,,,,,,,,,{name},,\n"
    )


def build_tvtime_export(tmp_path, extra=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "tracking-prod-records-v2.csv",
            V2_HEADER
            + v2_watch(99001, "Test Show", 1, 1, "2024-01-02 20:00:00")
            + v2_watch(99001, "Test Show", 1, 2, "2024-01-03 20:00:00")
            + v2_watch(99001, "Test Show", 9, 9, "2024-01-04 20:00:00")  # no such episode
            + v2_watch(42424242, "Unknown Show", 1, 1, "2024-01-05 20:00:00")
            + v2_series(99001, "Test Show", "true", "false")
            + v2_series(42424242, "Unknown Show", "true", "false"),
        )
        archive.writestr(
            "followed_tv_show.csv",
            "tv_show_id,updated_at,active,notification_type,tv_show_name,user_id,diffusion,"
            "folder_id,archived,notification_offset,created_at\n"
            "99001,2024-01-01 00:00:00,1,2,Test Show,1,original,,0,-60,2023-01-01 00:00:00\n",
        )
        # Files that must be ignored: none of these are viewing history.
        archive.writestr(
            "ip_address.csv",
            "user_id,ip,region_name,city_name,created_at\n1,10.0.0.1,California,Oakland,2024-01-01\n",
        )
        archive.writestr(
            "user_facebook_like.csv", "user_id,name,created_at\n1,Some Band,2016-01-01\n"
        )
        archive.writestr("access_token.csv", "user_id,token\n1,secret\n")
        archive.writestr(
            "tv_show_rate.csv",
            "updated_at,tv_show_name,user_id,tv_show_id,rating,created_at\n"
            "2024-01-01,Rated But Never Watched,1,55555,5,2024-01-01\n",
        )
        for name, body in extra:
            archive.writestr(name, body)
    path = tmp_path / "gdprdata.zip"
    path.write_bytes(buffer.getvalue())
    return path


def test_tvtime_export_is_recognised_and_narrowed_to_history(tmp_path):
    parsed = importer.parse_source(build_tvtime_export(tmp_path))

    assert parsed.format == "TV Time export"
    used = {item["file"]: item["used"] for item in parsed.files}
    assert "follow state" in used["tracking-prod-records-v2.csv"]
    assert "watch history" in used["tracking-prod-records-v2.csv"]
    assert "followed_tv_show.csv" in used

    ignored = next(item for item in parsed.files if item["file"].endswith("other files"))
    assert set(ignored["names"]) == {
        "ip_address.csv",
        "user_facebook_like.csv",
        "access_token.csv",
        "tv_show_rate.csv",
    }

    # Four watch rows, two shows, and nothing from the ignored files.
    assert len(parsed.watches) == 4
    assert {record.key.tvdb_id for record in parsed.shows.values()} == {99001, 42424242}


def test_tvtime_follow_flags_are_read(tmp_path):
    export = build_tvtime_export(
        tmp_path,
        extra=[
            (
                "user_tv_show_data.csv",
                "user_id,tv_show_id,is_followed,is_favorited,nb_episodes_seen,tv_show_name\n"
                "1,77777,0,0,0,Dropped Show\n",
            )
        ],
    )
    parsed = importer.parse_source(export)

    by_tvdb = {record.key.tvdb_id: record for record in parsed.shows.values()}
    assert by_tvdb[99001].followed is True
    assert by_tvdb[99001].archived is False
    assert by_tvdb[77777].followed is False


def test_tvtime_archive_state_takes_the_newest_source(tmp_path):
    """v2 is the current system; followed_tv_show.csv is the older one."""
    export = build_tvtime_export(
        tmp_path,
        extra=[
            (
                "seen_episode_source.csv",
                "tv_show_name,episode_season_number,episode_number,user_id,episode_id,"
                "source,created_at,updated_at\n"
                "Test Show,1,1,1,5,season-detail,2018-01-01 00:00:00,2018-01-01 00:00:00\n",
            )
        ],
    )
    parsed = importer.parse_source(export)
    # v2 says archived=false for 99001 even though it appears in other files.
    by_tvdb = {record.key.tvdb_id: record for record in parsed.shows.values()}
    assert by_tvdb[99001].archived is False
    # The older name-only file folds into the TVDB-keyed entry rather than
    # creating a second "Test Show".
    assert len(parsed.shows) == 2
    assert all(record.key.tvdb_id for record in parsed.shows.values())


def test_v2_rows_that_are_not_watches_are_not_counted(tmp_path):
    export = build_tvtime_export(
        tmp_path,
        extra=[
            (
                "recommendations-prod-user-shows.csv",
                "user_id,series_name,season_number,episode_number,created_at\n"
                "1,Recommended Show,1,1,2024-01-01\n",
            )
        ],
    )
    parsed = importer.parse_source(export)
    names = {record.key.name for record in parsed.shows.values()}
    assert "Recommended Show" not in names
    assert len(parsed.watches) == 4


# --------------------------------------------------------------------------
# importing
# --------------------------------------------------------------------------


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
    report = await importer.run_import(build_tvtime_export(tmp_path), dry_run=True)

    assert report["dry_run"] is True
    assert report["format"] == "TV Time export"
    assert report["shows_found"] == 1
    assert report["episodes_marked"] == 2
    assert report["episodes_unmatched"] == 1  # the bogus S09E09
    assert report["shows_unmatched"] == ["Unknown Show (tvdb 42424242)"]

    assert database.execute("SELECT COUNT(*) AS n FROM watch").fetchone()["n"] == 0
    assert database.execute("SELECT COUNT(*) AS n FROM follow").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_commit_writes_watches_and_follows(database, fake_tvmaze, tmp_path):
    report = await importer.run_import(build_tvtime_export(tmp_path), dry_run=False)

    assert report["episodes_marked"] == 2
    watches = database.execute(
        "SELECT episode_id, watched_at FROM watch ORDER BY episode_id"
    ).fetchall()
    assert [row["episode_id"] for row in watches] == [101, 102]
    assert watches[0]["watched_at"] == "2024-01-02T20:00:00+00:00"
    assert database.execute("SELECT COUNT(*) AS n FROM follow").fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_archived_shows_import_as_archived(database, fake_tvmaze, tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "tracking-prod-records-v2.csv",
            V2_HEADER
            + v2_watch(99001, "Test Show", 1, 1, "2024-01-02 20:00:00")
            + v2_series(99001, "Test Show", "true", "true"),
        )
    path = tmp_path / "archived.zip"
    path.write_bytes(buffer.getvalue())

    report = await importer.run_import(path, dry_run=False)
    assert report["shows_to_archive"] == 1
    assert report["shows_to_follow"] == 0
    row = database.execute("SELECT archived FROM follow").fetchone()
    assert row["archived"] == 1


@pytest.mark.asyncio
async def test_unfollowed_shows_keep_history_but_stay_out_of_the_library(
    database, fake_tvmaze, tmp_path
):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "tracking-prod-records-v2.csv",
            V2_HEADER
            + v2_watch(99001, "Test Show", 1, 1, "2024-01-02 20:00:00")
            + v2_series(99001, "Test Show", "false", "false"),
        )
    path = tmp_path / "unfollowed.zip"
    path.write_bytes(buffer.getvalue())

    await importer.run_import(path, dry_run=False)

    assert database.execute("SELECT COUNT(*) AS n FROM watch").fetchone()["n"] == 1
    assert database.execute("SELECT COUNT(*) AS n FROM follow").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_reimport_is_idempotent(database, fake_tvmaze, tmp_path):
    export = build_tvtime_export(tmp_path)
    await importer.run_import(export, dry_run=False)
    second = await importer.run_import(export, dry_run=False)

    assert second["episodes_marked"] == 0
    assert second["episodes_already_known"] == 2
    assert database.execute("SELECT COUNT(*) AS n FROM watch").fetchone()["n"] == 2


@pytest.mark.asyncio
async def test_rewatches_keep_the_earliest_watch_date(database, fake_tvmaze, tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "tracking-prod-records-v2.csv",
            V2_HEADER
            + v2_watch(99001, "Test Show", 1, 1, "2024-06-01 10:00:00")
            + v2_watch(99001, "Test Show", 1, 1, "2024-02-01 10:00:00", key="rewatch-episode-1"),
        )
    path = tmp_path / "rewatch.zip"
    path.write_bytes(buffer.getvalue())

    await importer.run_import(path, dry_run=False)
    row = database.execute("SELECT watched_at FROM watch WHERE episode_id = 101").fetchone()
    assert row["watched_at"] == "2024-02-01T10:00:00+00:00"


@pytest.mark.asyncio
async def test_progress_is_reported(database, fake_tvmaze, tmp_path):
    seen = []
    await importer.run_import(
        build_tvtime_export(tmp_path),
        dry_run=True,
        progress=lambda stage, done, total: seen.append((stage, done, total)),
    )
    matching = [item for item in seen if item[0] == "Matching shows against TVmaze"]
    assert matching, "show matching should report progress"
    done, total = matching[-1][1], matching[-1][2]
    assert done == total > 0, "progress should finish at 100%"


@pytest.mark.asyncio
async def test_plain_csv_still_imports(database, fake_tvmaze, tmp_path):
    path = tmp_path / "by-name.csv"
    path.write_text("series_name,season,episode,watched_at\nTest Show,1,1,2024-03-03\n")

    report = await importer.run_import(path, dry_run=False)

    assert report["format"] == "generic CSV"
    assert report["shows_found"] == 1
    assert report["episodes_marked"] == 1
    assert report["shows_guessed"] == []


@pytest.mark.asyncio
async def test_generic_import_follows_watched_shows(database, fake_tvmaze, tmp_path):
    path = tmp_path / "by-name.csv"
    path.write_text("series_name,season,episode,watched_at\nTest Show,1,1,2024-03-03\n")
    await importer.run_import(path, dry_run=False)

    assert database.execute("SELECT COUNT(*) AS n FROM follow").fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_import_records_a_job(database, fake_tvmaze, tmp_path):
    await importer.run_import(build_tvtime_export(tmp_path), dry_run=True, filename="export.zip")
    row = database.execute("SELECT filename, status FROM import_job").fetchone()

    assert row["filename"] == "export.zip"
    assert row["status"] == "preview"
