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


async def test_the_generation_is_not_recorded_by_a_pass_that_raised(database, monkeypatch, quiet_feed):
    """A pass cut short is retried whole next time, not left half done."""
    library.save_show(make_show(show_id=1))
    library.follow_show(1)

    async def cut_short(show_id):
        raise RuntimeError("container went away")

    monkeypatch.setattr(library, "sync_show", cut_short)
    with pytest.raises(RuntimeError):
        await refresh.refresh_all()
    assert get_meta("episode_sync_generation") is None
