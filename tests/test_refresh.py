"""A generation bump re-syncs every show once; then the pass goes incremental."""

import pytest

from app import library, refresh, tvmaze
from app.db import get_meta

from conftest import make_show


@pytest.fixture()
def quiet_feed(monkeypatch):
    async def no_updates(period):
        return {}

    monkeypatch.setattr(tvmaze, "updates_since", no_updates)


async def test_a_new_sync_generation_resyncs_a_fresh_show_once(database, monkeypatch, quiet_feed):
    """Staleness skips a show synced minutes ago. It must not skip one when the
    code has started collecting something it did not before."""
    library.save_show(make_show(show_id=1))  # synced_at is now: fresh
    library.follow_show(1)

    synced = []

    async def fake_sync(show_id):
        synced.append(show_id)
        return 0

    monkeypatch.setattr(library, "sync_show", fake_sync)

    # Nothing recorded, as on the first pass after this ships.
    assert get_meta("episode_sync_generation") is None
    report = await refresh.refresh_all()
    assert synced == [1]
    assert report["regenerated"] is True
    assert get_meta("episode_sync_generation") == refresh.SYNC_GENERATION

    # Same fresh show, generation now current: back to being skipped.
    synced.clear()
    report = await refresh.refresh_all()
    assert synced == []
    assert report["regenerated"] is False


async def test_the_generation_is_not_recorded_by_a_pass_that_was_cut_short(database, monkeypatch, quiet_feed):
    """A pass stopped partway - the process shutting down - is retried whole.

    Cancellation is the one thing that should still end a pass early; a show
    that merely fails is caught and retried on its own (below).
    """
    import asyncio

    library.save_show(make_show(show_id=1))
    library.follow_show(1)

    async def cut_short(show_id):
        raise asyncio.CancelledError()

    monkeypatch.setattr(library, "sync_show", cut_short)
    with pytest.raises(asyncio.CancelledError):
        await refresh.refresh_all()
    assert get_meta("episode_sync_generation") is None


async def test_one_failing_show_does_not_stop_the_rest(database, monkeypatch, quiet_feed):
    """A garbled payload is one show's problem, not the library's.

    It used to escape the loop: every show after it went unrefreshed, and in a
    regeneration pass the generation was never recorded, so each pass repeated
    the whole library and died at the same show.
    """
    import json

    from app.db import connect

    for show_id in (1, 2, 3):
        library.save_show(make_show(show_id=show_id))
        library.follow_show(show_id)

    attempted, synced = [], []

    async def sync(show_id):
        attempted.append(show_id)
        if show_id == 2:
            json.loads('{"id": 2, "na')  # a truncated body
        synced.append(show_id)

    monkeypatch.setattr(library, "sync_show", sync)
    report = await refresh.refresh_all()

    assert synced == [1, 3]
    assert report["failed"] == [2]
    assert get_meta("episode_sync_generation") == refresh.SYNC_GENERATION
    synced_at = {row["id"]: row["synced_at"] for row in connect().execute("SELECT id, synced_at FROM show")}
    assert synced_at[2] is None
    assert synced_at[1] is not None and synced_at[3] is not None

    # The next pass retries the failure, and only it: the generation is now
    # current and the other two are fresh.
    attempted.clear()
    await refresh.refresh_all()
    assert attempted == [2]
