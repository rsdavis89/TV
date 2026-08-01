from datetime import datetime, timedelta, timezone

from app import library

from conftest import make_episode, make_show


def stamp(days_from_now: int) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(days=days_from_now))
        .replace(microsecond=0)
        .isoformat()
    )


def seed(database, episodes=None, **show_kwargs):
    show = make_show(**show_kwargs)
    library.save_show(show)
    library.save_episodes(
        show["id"],
        episodes
        if episodes is not None
        else [
            make_episode(101, 1, 1, stamp(-30)),
            make_episode(102, 1, 2, stamp(-23)),
            make_episode(103, 1, 3, stamp(-16)),
            make_episode(104, 1, 4, stamp(3)),
        ],
    )
    library.follow_show(show["id"])
    return show["id"]


def test_next_episode_is_the_first_gap(database):
    show_id = seed(database)
    assert library.next_episode(show_id)["id"] == 101

    library.mark_watched(101)
    assert library.next_episode(show_id)["id"] == 102

    # Skipping ahead must not make the skipped episode disappear from the queue.
    library.mark_watched(103)
    assert library.next_episode(show_id)["id"] == 102


def test_progress_counts_only_aired_episodes_as_available(database):
    show_id = seed(database)
    library.mark_watched(101)
    library.mark_watched(102)

    progress = library.progress(show_id)
    assert progress["total"] == 4
    assert progress["aired"] == 3
    assert progress["watched"] == 2
    assert progress["remaining"] == 1
    assert progress["percent"] == 50
    assert progress["minutes"] == 90


def test_specials_are_excluded_from_progress_and_up_next(database):
    show_id = seed(
        database,
        episodes=[
            make_episode(201, 0, 1, stamp(-40), type="insignificant_special"),
            make_episode(202, 1, 1, stamp(-30)),
            make_episode(203, 1, 2, stamp(-20)),
        ],
    )
    assert library.progress(show_id)["total"] == 2
    assert library.next_episode(show_id)["id"] == 202


def test_status_moves_through_ready_scheduled_and_complete(database):
    show_id = seed(database)
    assert library.show_card(show_id)["status"] == "ready"

    for episode_id in (101, 102, 103):
        library.mark_watched(episode_id)
    assert library.show_card(show_id)["status"] == "scheduled"

    library.mark_watched(104)
    assert library.show_card(show_id)["status"] == "caught_up"

    library.save_show(make_show(status="Ended"))
    assert library.show_card(show_id)["status"] == "complete"


def test_mark_through_fills_every_earlier_episode(database):
    show_id = seed(database)
    marked = library.mark_through(show_id, 103)

    assert marked == 3
    assert library.progress(show_id)["watched"] == 3
    assert library.next_episode(show_id)["id"] == 104


def test_season_toggle_marks_and_unmarks(database):
    show_id = seed(
        database,
        episodes=[
            make_episode(301, 1, 1, stamp(-40)),
            make_episode(302, 1, 2, stamp(-33)),
            make_episode(303, 2, 1, stamp(-10)),
        ],
    )
    assert library.set_season_watched(show_id, 1, True) == 2
    assert library.progress(show_id)["watched"] == 2

    library.set_season_watched(show_id, 1, False)
    assert library.progress(show_id)["watched"] == 0


def test_last_watched_follows_watch_time_not_episode_order(database):
    show_id = seed(database)
    library.mark_watched(103, watched_at="2024-01-01T00:00:00+00:00")
    library.mark_watched(101, watched_at="2024-06-01T00:00:00+00:00")

    assert library.last_watched_episode(show_id)["id"] == 101


def test_home_groups_shows_by_what_you_can_do(database):
    started_id = seed(database, show_id=1, name="Started Show")
    library.mark_watched(101)
    seed(
        database,
        show_id=2,
        name="Upcoming Show",
        episodes=[make_episode(401, 1, 1, stamp(5), show_id=2)],
    )
    seed(
        database,
        show_id=3,
        name="Never Started",
        episodes=[make_episode(501, 1, 1, stamp(-2), show_id=3)],
    )

    home = library.home()
    assert [card["show"]["id"] for card in home["ready"]] == [started_id]
    assert [card["show"]["id"] for card in home["scheduled"]] == [2]
    assert [card["show"]["id"] for card in home["not_started"]] == [3]
    assert home["counts"]["ready"] == 1
    assert home["counts"]["not_started"] == 1


def test_ready_shows_are_ordered_by_when_you_last_watched(database):
    seed(database, show_id=1, name="Older")
    seed(
        database,
        show_id=2,
        name="Newer",
        episodes=[
            make_episode(601, 1, 1, stamp(-40), show_id=2),
            make_episode(602, 1, 2, stamp(-39), show_id=2),
        ],
    )
    library.mark_watched(101, watched_at="2024-01-01T00:00:00+00:00")
    library.mark_watched(601, watched_at="2024-09-01T00:00:00+00:00")

    assert [card["show"]["name"] for card in library.home()["ready"]] == ["Newer", "Older"]


def test_upcoming_only_lists_future_airings_of_followed_shows(database):
    seed(database)
    upcoming = library.upcoming(days=10)
    assert [episode["id"] for episode in upcoming] == [104]

    assert library.upcoming(days=1) == []


def test_new_since_skips_watched_and_future_episodes(database):
    seed(database)
    fresh = library.new_since(stamp(-25))

    assert [episode["id"] for episode in fresh] == [103, 102]

    library.mark_watched(103)
    assert [episode["id"] for episode in library.new_since(stamp(-25))] == [102]


def test_save_episodes_drops_removed_episodes_but_keeps_watched_ones(database):
    show_id = seed(database)
    library.mark_watched(101)

    library.save_episodes(show_id, [make_episode(102, 1, 2, stamp(-23))])

    remaining = {
        row["id"]
        for row in database.execute("SELECT id FROM episode WHERE show_id = ?", (show_id,))
    }
    assert remaining == {101, 102}


def test_unfollow_can_keep_or_purge_history(database):
    show_id = seed(database)
    library.mark_watched(101)

    library.unfollow_show(show_id)
    assert database.execute("SELECT COUNT(*) AS n FROM watch").fetchone()["n"] == 1

    library.follow_show(show_id)
    library.unfollow_show(show_id, keep_history=False)
    assert database.execute("SELECT COUNT(*) AS n FROM watch").fetchone()["n"] == 0


def test_summary_html_is_stripped(database):
    seed(database)
    row = database.execute("SELECT summary FROM show WHERE id = 1").fetchone()
    assert row["summary"] == "A show."


def test_airstamp_is_normalised_to_utc(database):
    show_id = seed(
        database,
        episodes=[make_episode(501, 1, 1, "2024-03-01T21:00:00-05:00")],
    )
    row = database.execute("SELECT airstamp FROM episode WHERE id = 501").fetchone()
    assert row["airstamp"] == "2024-03-02T02:00:00+00:00"


def test_stats_totals(database):
    seed(database)
    library.mark_watched(101)
    library.mark_watched(102)

    stats = library.stats()
    assert stats["episodes"] == 2
    assert stats["minutes"] == 90
    assert stats["shows"] == 1
    assert stats["following"] == 1


def test_gaps_before_furthest_counts_skipped_episodes(database):
    show_id = seed(database)
    assert library.gaps_before_furthest(show_id) == 0

    # Watched the third episode only: two earlier ones are holes.
    library.mark_watched(103)
    assert library.gaps_before_furthest(show_id) == 2
    assert library.show_card(show_id)["gaps"] == 2

    library.mark_through(show_id, 103)
    assert library.gaps_before_furthest(show_id) == 0
