"""The CLI reads the same groups as the web UI, so it must not miss any."""

from app import cli, library

from conftest import make_episode
from test_library import seed


def args(**kwargs):
    return type("Args", (), {"upcoming": 0, **kwargs})()


def test_next_lists_priority_shows_first(database, capsys):
    seed(database, show_id=1, name="Ordinary Show")
    seed(
        database,
        show_id=2,
        name="Pinned Show",
        episodes=[
            make_episode(701, 1, 1, "2024-01-01T00:00:00+00:00", show_id=2),
            make_episode(702, 1, 2, "2024-01-08T00:00:00+00:00", show_id=2),
        ],
    )
    library.mark_watched(101)  # Ordinary Show is in progress
    library.mark_watched(701)  # Pinned Show is in progress
    library.set_priority(2, True)

    cli.cmd_next(args())
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert lines[0].startswith("*") and "Pinned Show" in lines[0]
    assert "Ordinary Show" in lines[1]


def test_next_counts_unstarted_shows_instead_of_listing_them(database, capsys):
    seed(database, show_id=1, name="In Progress")
    library.mark_watched(101)
    seed(
        database,
        show_id=2,
        name="Never Opened",
        episodes=[make_episode(801, 1, 1, "2024-01-01T00:00:00+00:00", show_id=2)],
    )

    cli.cmd_next(args())
    out = capsys.readouterr().out

    assert "In Progress" in out
    assert "Never Opened" not in out
    assert "1 followed show(s) not started yet." in out


def test_next_says_so_when_nothing_is_in_progress(database, capsys):
    cli.cmd_next(args())
    assert "Nothing in progress" in capsys.readouterr().out
