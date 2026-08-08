"""Premiere sweeping: the schedule walk that finds things worth adding."""

import json

import pytest

from app import library, premieres
from app.db import connect, tx, utcnow


def schedule_item(episode_id, show_id, name, season, number, airstamp,
                  channel="Netflix", language="English", web=True):
    channel_field = "webChannel" if web else "network"
    return {
        "id": episode_id,
        "season": season,
        "number": number,
        "airstamp": airstamp,
        "_embedded": {
            "show": {
                "id": show_id,
                "name": name,
                "language": language,
                "genres": ["Drama"],
                "status": "Running",
                "summary": "<p>A <b>show</b>.</p>",
                "image": {"medium": "http://example.com/m.jpg"},
                "rating": {"average": 8.1},
                "averageRuntime": 50,
                channel_field: {"name": channel},
            }
        },
    }


def test_only_first_episodes_count_as_premieres():
    assert premieres._normalise(schedule_item(1, 10, "A", 1, 1, "2026-08-01T00:00:00+00:00"))
    # Episode 4 of a season is not a premiere.
    assert premieres._normalise(schedule_item(2, 10, "A", 1, 4, "2026-08-01T00:00:00+00:00")) is None


def test_season_one_is_a_new_show_and_anything_else_is_a_return():
    new = premieres._normalise(schedule_item(1, 10, "A", 1, 1, "2026-08-01T00:00:00+00:00"))
    back = premieres._normalise(schedule_item(2, 11, "B", 4, 1, "2026-08-01T00:00:00+00:00"))
    assert new["kind"] == "series"
    assert back["kind"] == "season"
    assert back["season"] == 4


def test_non_english_and_channelless_entries_are_dropped():
    assert premieres._normalise(
        schedule_item(1, 10, "A", 1, 1, "2026-08-01T00:00:00+00:00", language="Japanese")
    ) is None

    item = schedule_item(2, 11, "B", 1, 1, "2026-08-01T00:00:00+00:00")
    item["_embedded"]["show"].pop("webChannel")
    assert premieres._normalise(item) is None


def test_broadcast_entries_use_the_network_name():
    row = premieres._normalise(
        schedule_item(1, 10, "A", 1, 1, "2026-08-01T00:00:00+00:00", channel="ABC", web=False)
    )
    assert row["channel"] == "ABC"


def test_summary_html_is_stripped_from_premieres():
    row = premieres._normalise(schedule_item(1, 10, "A", 1, 1, "2026-08-01T00:00:00+00:00"))
    assert row["summary"] == "A show."


def store(database, rows):
    with tx() as conn:
        for row in rows:
            columns = ", ".join(row)
            marks = ", ".join(f":{k}" for k in row)
            conn.execute(f"INSERT OR REPLACE INTO premiere ({columns}) VALUES ({marks})", row)


def make_row(episode_id, show_id, name, season, airstamp, channel="Netflix"):
    return {
        "episode_id": episode_id, "show_id": show_id, "show_name": name,
        "season": season, "airstamp": airstamp, "channel": channel,
        "kind": "series" if season == 1 else "season",
        "genres": json.dumps(["Drama"]), "summary": None, "image": None,
        "show_status": "Running", "rating": 8.0, "runtime": 50, "fetched_at": utcnow(),
    }


def test_listing_hides_shows_you_already_follow(database):
    from test_library import seed, stamp

    seed(database, show_id=1, name="Followed")
    store(database, [
        make_row(900, 1, "Followed", 3, stamp(-2)),
        make_row(901, 77, "Stranger", 1, stamp(-1)),
    ])

    listing = premieres.listing(back_days=14, ahead_days=21)
    assert [p["show_id"] for p in listing["premieres"]] == [77]

    # But the channel tally still counts everything found.
    assert dict(listing["channels"])["Netflix"] == 2

    everything = premieres.listing(back_days=14, ahead_days=21, include_followed=True)
    assert len(everything["premieres"]) == 2


def test_listing_respects_the_window_and_orders_newest_first(database):
    from test_library import stamp

    store(database, [
        make_row(900, 70, "Long ago", 1, stamp(-200)),
        make_row(901, 71, "Recent", 1, stamp(-3)),
        make_row(902, 72, "Soon", 1, stamp(4)),
        make_row(903, 73, "Far off", 1, stamp(90)),
    ])

    names = [p["show_name"] for p in premieres.listing(back_days=14, ahead_days=21)["premieres"]]
    assert names == ["Soon", "Recent"]


def test_listing_marks_what_has_already_aired(database):
    from test_library import stamp

    store(database, [
        make_row(900, 70, "Out", 1, stamp(-1)),
        make_row(901, 71, "Upcoming", 1, stamp(3)),
    ])

    by_name = {p["show_name"]: p for p in premieres.listing()["premieres"]}
    assert by_name["Out"]["aired"] is True
    assert by_name["Upcoming"]["aired"] is False


def test_prune_drops_only_ancient_rows(database):
    from test_library import stamp

    store(database, [
        make_row(900, 70, "Ancient", 1, stamp(-200)),
        make_row(901, 71, "Recent", 1, stamp(-5)),
    ])

    assert premieres.prune(keep_days=120) == 1
    remaining = database.execute("SELECT show_name FROM premiere").fetchall()
    assert [r["show_name"] for r in remaining] == ["Recent"]


def test_sweep_is_rate_limited_between_runs(database):
    from app.db import set_meta

    assert premieres.due_for_sweep() is True
    set_meta("premieres_swept_at", utcnow())
    set_meta("premieres_sweep_generation", premieres.SWEEP_GENERATION)
    assert premieres.due_for_sweep() is False


def test_a_new_sweep_generation_overrides_the_rate_limit(database):
    """Otherwise a deploy that widens the horizon serves stale, narrower data."""
    from app.db import set_meta

    set_meta("premieres_swept_at", utcnow())
    set_meta("premieres_sweep_generation", premieres.SWEEP_GENERATION)
    assert premieres.due_for_sweep() is False

    set_meta("premieres_sweep_generation", "1")
    assert premieres.due_for_sweep() is True


@pytest.fixture()
def fake_schedule(monkeypatch):
    """Both halves of the sweep, stubbed. No test should touch the network."""
    from test_library import stamp

    async def full_schedule():
        return [schedule_item(600, 70, "Upcoming Thing", 1, 1, stamp(9))]

    async def fake_get(path, params=None, attempts=4):
        today = premieres.datetime.now(premieres.timezone.utc).date().isoformat()
        if (params or {}).get("date") != today:
            return []
        return [
            schedule_item(500, 60, "Fresh Thing", 1, 1, stamp(0)),
            schedule_item(501, 61, "Returning Thing", 2, 1, stamp(0)),
            schedule_item(502, 62, "Mid-season", 1, 6, stamp(0)),
        ]

    monkeypatch.setattr(premieres.tvmaze, "full_schedule", full_schedule)
    monkeypatch.setattr(premieres.tvmaze, "_get", fake_get)


@pytest.mark.asyncio
async def test_sweep_stores_past_and_future(database, fake_schedule):
    report = await premieres.sweep(back_days=0)

    # Two from today's schedules (deduped across both endpoints) plus one ahead.
    assert report["found"] == 3
    rows = database.execute("SELECT show_name, kind FROM premiere ORDER BY episode_id").fetchall()
    assert [(r["show_name"], r["kind"]) for r in rows] == [
        ("Fresh Thing", "series"),
        ("Returning Thing", "season"),
        ("Upcoming Thing", "series"),
    ]


@pytest.mark.asyncio
async def test_sweep_survives_the_full_schedule_failing(database, monkeypatch):
    """The big request is the fragile one; the daily walk must still run."""
    from test_library import stamp

    async def boom():
        raise RuntimeError("10 MB is a lot to ask for")

    async def fake_get(path, params=None, attempts=4):
        day = (params or {}).get("date")
        today = premieres.datetime.now(premieres.timezone.utc).date()
        if day == today.isoformat():
            return [schedule_item(500, 60, "Fresh Thing", 1, 1, stamp(0))]
        if day == (today + premieres.timedelta(days=5)).isoformat():
            return [schedule_item(501, 61, "Later Thing", 1, 1, stamp(5))]
        return []

    monkeypatch.setattr(premieres.tvmaze, "full_schedule", boom)
    monkeypatch.setattr(premieres.tvmaze, "_get", fake_get)

    # The daily walk covers the future too when the big request is unavailable,
    # so a failure narrows the horizon rather than emptying it.
    report = await premieres.sweep(back_days=0, ahead_days=7)
    assert report["found"] == 2
    assert report["failed_requests"] == 1
    assert report["full_schedule"] is False


@pytest.mark.asyncio
async def test_a_working_full_schedule_skips_the_daily_future_walk(database, monkeypatch):
    from test_library import stamp

    asked = []

    async def full_schedule():
        return [schedule_item(600, 70, "Upcoming Thing", 1, 1, stamp(40))]

    async def fake_get(path, params=None, attempts=4):
        asked.append((params or {}).get("date"))
        return []

    monkeypatch.setattr(premieres.tvmaze, "full_schedule", full_schedule)
    monkeypatch.setattr(premieres.tvmaze, "_get", fake_get)

    report = await premieres.sweep(back_days=2)
    assert report["full_schedule"] is True
    # Three days back through today, two schedules each, and nothing forward.
    assert len(asked) == 6
    assert report["horizon"] == premieres.horizon()


@pytest.mark.asyncio
async def test_sweep_records_when_it_last_ran(database, fake_schedule):
    assert premieres.listing()["swept_at"] is None
    await premieres.sweep(back_days=0)
    assert premieres.listing()["swept_at"] is not None


# --------------------------------------------------------------------------
# dismissing a premiere you have passed on
# --------------------------------------------------------------------------


def test_a_dismissed_premiere_drops_out_of_the_listing(database):
    from test_library import stamp

    store(database, [
        make_row(900, 70, "Keep", 1, stamp(1)),
        make_row(901, 71, "Pass", 1, stamp(2)),
    ])

    assert premieres.dismiss(901) is True
    listing = premieres.listing()
    assert [p["show_name"] for p in listing["premieres"]] == ["Keep"]
    # Counted so the tab can offer them back.
    assert listing["hidden"] == 1

    everything = premieres.listing(include_dismissed=True)
    assert {p["show_name"]: p["dismissed"] for p in everything["premieres"]} == {
        "Keep": False, "Pass": True
    }


def test_dismissing_something_that_is_not_there_reports_it(database):
    assert premieres.dismiss(404404) is False


def test_a_dismissal_can_be_undone_one_at_a_time_or_all_at_once(database):
    from test_library import stamp

    store(database, [
        make_row(900, 70, "One", 1, stamp(1)),
        make_row(901, 71, "Two", 1, stamp(2)),
    ])
    premieres.dismiss(900)
    premieres.dismiss(901)
    assert premieres.listing()["premieres"] == []

    premieres.dismiss(900, value=False)
    assert [p["show_name"] for p in premieres.listing()["premieres"]] == ["One"]

    assert premieres.restore_all() == 1
    assert len(premieres.listing()["premieres"]) == 2
    # Nothing left to restore.
    assert premieres.restore_all() == 0


@pytest.mark.asyncio
async def test_a_sweep_does_not_undismiss_what_you_passed_on(database, fake_schedule):
    """The sweep re-upserts every row it finds; the dismissal must survive."""
    await premieres.sweep(back_days=0)
    [row] = database.execute("SELECT episode_id FROM premiere LIMIT 1").fetchall()
    premieres.dismiss(row["episode_id"])
    assert premieres.listing()["hidden"] == 1

    await premieres.sweep(back_days=0)
    assert premieres.listing()["hidden"] == 1
