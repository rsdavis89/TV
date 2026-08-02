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


def test_favorite_and_priority_flags_round_trip(database):
    show_id = seed(database)
    card = library.show_card(show_id)
    assert card["favorite"] is False and card["priority"] is False

    library.set_favorite(show_id, True)
    library.set_priority(show_id, True)
    card = library.show_card(show_id)
    assert card["favorite"] is True and card["priority"] is True

    library.set_priority(show_id, False)
    assert library.show_card(show_id)["priority"] is False
    assert library.show_card(show_id)["favorite"] is True


def test_priority_does_not_reorder_up_next(database):
    """Pinning is a shortlist you visit, not a promotion on the main screen."""
    seed(database, show_id=1, name="Recently Watched")
    seed(
        database,
        show_id=2,
        name="Pinned",
        episodes=[
            make_episode(701, 1, 1, stamp(-40), show_id=2),
            make_episode(702, 1, 2, stamp(-39), show_id=2),
        ],
    )
    library.mark_watched(101, watched_at="2024-09-01T00:00:00+00:00")
    library.mark_watched(701, watched_at="2024-01-01T00:00:00+00:00")
    library.set_priority(2, True)

    home = library.home()
    assert "priority" not in home
    # Both appear in Ready to watch, ordered only by when they were last watched.
    assert [c["show"]["name"] for c in home["ready"]] == ["Recently Watched", "Pinned"]
    assert home["counts"]["episodes_ready"] == (
        library.progress(1)["remaining"] + library.progress(2)["remaining"]
    )


def test_pinning_an_unstarted_show_leaves_it_in_not_started(database):
    """The clutter this replaced: a pinned show you have never opened."""
    seed(database, show_id=1, name="Started Show")
    library.mark_watched(101)
    seed(
        database,
        show_id=2,
        name="Pinned But Unopened",
        episodes=[make_episode(901, 1, 1, stamp(-5), show_id=2)],
    )
    library.set_priority(2, True)

    home = library.home()
    assert [c["show"]["name"] for c in home["ready"]] == ["Started Show"]
    assert [c["show"]["name"] for c in home["not_started"]] == ["Pinned But Unopened"]


def test_home_counts_how_many_pinned_shows_have_something_waiting(database):
    seed(database, show_id=1, name="Pinned With Episode")
    seed(
        database,
        show_id=2,
        name="Pinned And Caught Up",
        episodes=[make_episode(801, 1, 1, stamp(5), show_id=2)],
    )
    library.set_priority(1, True)
    library.set_priority(2, True)

    # Only the one with an aired, unwatched episode counts towards the badge.
    assert library.home()["counts"]["priority_waiting"] == 1


def test_bulk_archive_and_unarchive(database):
    for show_id in (1, 2, 3):
        seed(database, show_id=show_id, name=f"Show {show_id}",
             episodes=[make_episode(1000 + show_id, 1, 1, stamp(-5), show_id=show_id)])

    result = library.bulk_update([1, 2], "archive")
    assert result["changed"] == 2 and result["verb"] == "archived"
    assert library.followed_ids() == [3]

    library.bulk_update([1], "unarchive")
    assert sorted(library.followed_ids()) == [1, 3]


def test_bulk_unfollow_keeps_watch_history(database):
    show_id = seed(database)
    library.mark_watched(101)

    assert library.bulk_update([show_id], "unfollow")["changed"] == 1
    assert library.followed_ids() == []
    assert database.execute("SELECT COUNT(*) AS n FROM watch").fetchone()["n"] == 1


def test_bulk_flags(database):
    for show_id in (1, 2):
        seed(database, show_id=show_id, name=f"Show {show_id}",
             episodes=[make_episode(1000 + show_id, 1, 1, stamp(-5), show_id=show_id)])

    library.bulk_update([1, 2], "favorite")
    library.bulk_update([2], "priority")

    assert library.show_card(1)["favorite"] is True
    assert library.show_card(2)["priority"] is True
    assert library.show_card(1)["priority"] is False

    library.bulk_update([1, 2], "unfavorite")
    assert library.show_card(2)["favorite"] is False


def test_bulk_rejects_unknown_actions_and_tolerates_empty_input(database):
    seed(database)
    import pytest

    with pytest.raises(ValueError):
        library.bulk_update([1], "delete_everything")
    assert library.bulk_update([], "archive")["changed"] == 0


def test_unstarted_ids_finds_shows_with_nothing_watched(database):
    seed(database, show_id=1, name="Started")
    seed(database, show_id=2, name="Untouched",
         episodes=[make_episode(2001, 1, 1, stamp(-5), show_id=2)])
    library.mark_watched(101)

    assert library.unstarted_ids() == [2]

    library.set_archived(2, True)
    assert library.unstarted_ids() == []
    assert library.unstarted_ids(include_archived=True) == [2]


def test_calendar_looks_backwards_as_well_as_forwards(database):
    seed(
        database,
        episodes=[
            make_episode(901, 1, 1, stamp(-200)),   # outside a 90 day window
            make_episode(902, 1, 2, stamp(-40)),
            make_episode(903, 1, 3, stamp(-2)),
            make_episode(904, 1, 4, stamp(5)),
        ],
    )

    window = library.calendar(back_days=90, forward_days=35)

    assert [e["id"] for e in window["upcoming"]] == [904]
    # Most recent first, and the 200-day-old episode is outside the window.
    assert [e["id"] for e in window["recent"]] == [903, 902]
    assert window["back_days"] == 90


def test_calendar_marks_which_past_episodes_you_watched(database):
    seed(
        database,
        episodes=[
            make_episode(911, 1, 1, stamp(-10)),
            make_episode(912, 1, 2, stamp(-3)),
        ],
    )
    library.mark_watched(911)

    window = library.calendar(back_days=30, forward_days=0)

    by_id = {e["id"]: e for e in window["recent"]}
    assert by_id[911]["watched"] is True
    assert by_id[912]["watched"] is False
    assert window["unwatched_recent"] == 1


def test_calendar_with_no_lookback_matches_upcoming(database):
    seed(database)
    window = library.calendar(back_days=0, forward_days=10)

    assert window["recent"] == []
    assert [e["id"] for e in window["upcoming"]] == [e["id"] for e in library.upcoming(days=10)]


def test_calendar_caps_a_long_lookback_keeping_the_newest(database):
    episodes = [make_episode(1000 + i, 1, i + 1, stamp(-i - 1)) for i in range(10)]
    seed(database, episodes=episodes)

    window = library.calendar(back_days=365, forward_days=0, limit=4)

    assert len(window["recent"]) == 4
    assert window["truncated"] is True
    # The four kept are the most recent, not the oldest.
    assert [e["id"] for e in window["recent"]] == [1000, 1001, 1002, 1003]


def test_calendar_ignores_archived_shows(database):
    seed(database, episodes=[make_episode(921, 1, 1, stamp(-5))])
    assert len(library.calendar(back_days=30, forward_days=0)["recent"]) == 1

    library.set_archived(1, True)
    assert library.calendar(back_days=30, forward_days=0)["recent"] == []


def test_forget_unused_shows_clears_only_browsing_residue(database):
    followed = seed(database, show_id=1, name="Followed")
    # Watched but later removed from the library: history must survive.
    seed(database, show_id=2, name="Removed But Watched",
         episodes=[make_episode(2101, 1, 1, stamp(-5), show_id=2)])
    library.mark_watched(2101)
    library.unfollow_show(2)
    # Only ever looked at from search: no follow row, nothing watched.
    library.save_show(make_show(show_id=3, name="Just Peeked At"))
    library.save_episodes(3, [make_episode(3101, 1, 1, stamp(-5), show_id=3)])

    assert library.forget_unused_shows() == 1

    remaining = {row["id"] for row in database.execute("SELECT id FROM show")}
    assert remaining == {followed, 2}
    # Its episodes went with it.
    assert database.execute(
        "SELECT COUNT(*) AS n FROM episode WHERE show_id = 3"
    ).fetchone()["n"] == 0
    # And the removed-but-watched show kept its history.
    assert database.execute("SELECT COUNT(*) AS n FROM watch").fetchone()["n"] == 1


def test_forget_unused_shows_is_a_no_op_on_a_tidy_library(database):
    seed(database)
    assert library.forget_unused_shows() == 0


# --------------------------------------------------------------------------
# cast
# --------------------------------------------------------------------------


def cast_entry(person_id, name, character, image="http://x/p.jpg", **flags):
    return {
        "person": {"id": person_id, "name": name, "image": {"medium": image}},
        "character": {"id": person_id * 10, "name": character},
        **flags,
    }


def test_top_billing_keeps_only_what_the_page_shows():
    [member] = library.top_billing([cast_entry(1, "Ada", "Captain")])
    assert member == {
        "person_id": 1,
        "name": "Ada",
        "characters": ["Captain"],
        "image": "http://x/p.jpg",
        "self": False,
        "voice": False,
    }


def test_an_actor_with_two_roles_appears_once():
    """TVmaze lists a role per entry, which would spend two slots on one face."""
    billing = library.top_billing([
        cast_entry(1, "Ada", "Captain"),
        cast_entry(1, "Ada", "The Twin"),
        cast_entry(2, "Bo", "Cook"),
    ])
    assert [m["name"] for m in billing] == ["Ada", "Bo"]
    assert billing[0]["characters"] == ["Captain", "The Twin"]


def test_top_billing_is_capped_but_still_folds_late_duplicates():
    entries = [cast_entry(i, f"Person {i}", f"Role {i}") for i in range(1, 15)]
    entries.append(cast_entry(1, "Person 1", "Another Role"))

    billing = library.top_billing(entries)
    assert len(billing) == library.CAST_KEPT
    assert billing[0]["characters"] == ["Role 1", "Another Role"]


def test_entries_without_a_person_are_skipped():
    assert library.top_billing([{"character": {"name": "Nobody"}}]) == []
    assert library.top_billing([{"person": {"id": 5}}]) == []


def test_cast_is_stored_from_an_embedded_payload(database):
    from conftest import make_show

    library.save_show({
        **make_show(show_id=7, name="Embedded"),
        "_embedded": {"cast": [cast_entry(1, "Ada", "Captain")]},
    })
    show = library.show_public(library._show_row(7))
    assert [m["name"] for m in show["cast"]] == ["Ada"]


def test_a_payload_without_cast_does_not_blank_what_is_stored(database):
    """Imports and refreshes work from slimmer payloads; they must not wipe it."""
    from conftest import make_show

    library.save_show({
        **make_show(show_id=7, name="Embedded"),
        "_embedded": {"cast": [cast_entry(1, "Ada", "Captain")]},
    })
    library.save_show(make_show(show_id=7, name="Embedded"))

    show = library.show_public(library._show_row(7))
    assert [m["name"] for m in show["cast"]] == ["Ada"]


def test_never_fetched_cast_is_none_rather_than_empty(database):
    """The two are different: one is worth a lookup, the other is not."""
    from conftest import make_show

    library.save_show(make_show(show_id=7, name="Unknown"))
    assert library.show_public(library._show_row(7))["cast"] is None

    library.save_cast(7, [])
    assert library.show_public(library._show_row(7))["cast"] == []
