"""Command line helpers: import an export, refresh, or inspect the library."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from . import db, importer, library, refresh, tvmaze


def _print_report(report: dict) -> None:
    print(f"\n{'PREVIEW' if report['dry_run'] else 'IMPORT'} summary")
    print("-" * 46)
    for item in report["files"]:
        print(f"  {item['file']}: {item['rows']} rows -> {item['used']}")
    print(f"  detected format    : {report['format']}")
    print(f"  shows matched      : {report['shows_found']}")
    print(f"  shows to follow    : {report['shows_to_follow']}")
    print(f"  shows to archive   : {report['shows_to_archive']}")
    print(f"  watch rows read    : {report['watch_rows_read']}")
    print(f"  episodes marked    : {report['episodes_marked']}")
    print(f"  already known      : {report['episodes_already_known']}")
    print(f"  episodes unmatched : {report['episodes_unmatched']}")
    print(f"  unnumbered specials: {report['specials_skipped']}")
    if report["shows_unmatched"]:
        print("\n  Shows that could not be matched:")
        for name in report["shows_unmatched"][:30]:
            print(f"    - {name}")
    if report["shows_guessed"]:
        print("\n  Matched by name only, worth a look:")
        for name in report["shows_guessed"][:30]:
            print(f"    - {name}")
    if report["episodes_unmatched_sample"]:
        print("\n  Episodes with no counterpart on TVmaze:")
        for name in report["episodes_unmatched_sample"][:20]:
            print(f"    - {name}")


async def cmd_import(args: argparse.Namespace) -> None:
    source = Path(args.path).expanduser()
    if not source.exists():
        raise SystemExit(f"No such file or directory: {source}")
    last = [""]

    def progress(stage: str, done: int, total: int) -> None:
        line = f"{stage}: {done}/{total}" if total else stage
        if line != last[0]:
            print(f"\r  {line:<60}", end="", flush=True)
            last[0] = line

    report = await importer.run_import(
        source, dry_run=not args.commit, follow_shows=not args.no_follow, progress=progress
    )
    print()
    _print_report(report)
    if not args.commit:
        print("\nNothing was written. Re-run with --commit to apply.")


async def cmd_refresh(args: argparse.Namespace) -> None:
    print(json.dumps(await refresh.refresh_all(force=args.force), indent=2))


async def cmd_add(args: argparse.Namespace) -> None:
    results = await tvmaze.search_shows(args.query)
    if not results:
        raise SystemExit("No matches on TVmaze")
    show = results[0]
    await library.add_show(int(show["id"]))
    print(f"Added {show['name']} ({show.get('premiered') or 'n/a'})")


def cmd_next(args: argparse.Namespace) -> None:
    data = library.home()
    # Priority first, then shows in progress. Shows you have never started are
    # counted rather than listed; there can be hundreds of them.
    waiting = [*data["priority"], *data["ready"]]
    if not waiting:
        print("Nothing in progress. Check 'scheduled' for what is coming.")
    for card in waiting:
        nxt = card["next"]
        pin = "*" if card["priority"] else " "
        print(f"{pin} {card['show']['name']:<38} {nxt['code']}  {nxt['name'] or ''}")

    not_started = data["counts"]["not_started"]
    if not_started:
        print(f"\n{not_started} followed show(s) not started yet.")
    if args.upcoming:
        print("\nUpcoming:")
        for episode in library.upcoming(args.upcoming):
            print(
                f"{(episode['airstamp'] or '')[:10]}  "
                f"{episode['show_name']:<34} {episode['code']}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(prog="tv", description="Self-hosted TV tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="import a TV Time export (zip, folder or csv)")
    p_import.add_argument("path")
    p_import.add_argument(
        "--commit", action="store_true", help="write the changes (default is a preview)"
    )
    p_import.add_argument(
        "--no-follow", action="store_true", help="do not follow the imported shows"
    )
    p_import.set_defaults(func=cmd_import, is_async=True)

    p_refresh = sub.add_parser("refresh", help="check TVmaze for new episodes now")
    p_refresh.add_argument("--force", action="store_true", help="re-sync every followed show")
    p_refresh.set_defaults(func=cmd_refresh, is_async=True)

    p_add = sub.add_parser("add", help="follow the best search match for a title")
    p_add.add_argument("query")
    p_add.set_defaults(func=cmd_add, is_async=True)

    p_next = sub.add_parser("next", help="show what is ready to watch")
    p_next.add_argument("--upcoming", type=int, default=0, metavar="DAYS")
    p_next.set_defaults(func=cmd_next, is_async=False)

    args = parser.parse_args()
    db.migrate()
    if getattr(args, "is_async", False):
        asyncio.run(args.func(args))
    else:
        args.func(args)


if __name__ == "__main__":
    main()
