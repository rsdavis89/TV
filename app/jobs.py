"""Background import jobs.

A full TV Time library is several hundred shows, and each one costs a couple of
TVmaze calls under a rate limit, so an import runs for minutes rather than
seconds. Running it inside the upload request would time out, so the upload
starts a job and the page polls it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from . import importer
from .db import connect, tx, utcnow

log = logging.getLogger("tv.jobs")

_tasks: set[asyncio.Task] = set()


def create_job(filename: str | None, dry_run: bool) -> int:
    with tx() as conn:
        cursor = conn.execute(
            "INSERT INTO import_job (created_at, filename, dry_run, status, stage) "
            "VALUES (?, ?, ?, 'running', 'Queued')",
            (utcnow(), filename, 1 if dry_run else 0),
        )
        return int(cursor.lastrowid)


def update_progress(job_id: int, stage: str, done: int, total: int) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE import_job SET stage = ?, done = ?, total = ? WHERE id = ?",
            (stage, done, total, job_id),
        )


def fail_job(job_id: int, message: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE import_job SET status = 'failed', stage = 'Failed', error = ? WHERE id = ?",
            (message, job_id),
        )


def get_job(job_id: int) -> dict | None:
    row = connect().execute("SELECT * FROM import_job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    try:
        job["report"] = json.loads(job["report"]) if job["report"] else None
    except json.JSONDecodeError:
        job["report"] = None
    return job


async def _run(job_id: int, path: Path, dry_run: bool, follow_shows: bool) -> None:
    def progress(stage: str, done: int, total: int) -> None:
        update_progress(job_id, stage, done, total)

    try:
        await importer.run_import(
            path,
            dry_run=dry_run,
            follow_shows=follow_shows,
            progress=progress,
            job_id=job_id,
        )
    except Exception as exc:  # surfaced to the page, not just the log
        log.exception("import job %s failed", job_id)
        fail_job(job_id, f"{type(exc).__name__}: {exc}")
    finally:
        path.unlink(missing_ok=True)


def start_import(path: Path, *, filename: str | None, dry_run: bool, follow_shows: bool) -> int:
    job_id = create_job(filename, dry_run)
    task = asyncio.create_task(_run(job_id, path, dry_run, follow_shows))
    # Hold a reference so the task is not garbage collected mid-flight.
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job_id
