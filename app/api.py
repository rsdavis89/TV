"""HTTP API consumed by the web front end."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import auth, backup, config, importer, jobs, library, refresh, storage, tvmaze
from .db import connect, get_meta, tx, utcnow

router = APIRouter(prefix="/api")


class LoginBody(BaseModel):
    password: str = ""


class AddShowBody(BaseModel):
    tvmaze_id: int


class WatchBody(BaseModel):
    watched_at: str | None = None


class FlagBody(BaseModel):
    value: bool = True


class ThroughBody(BaseModel):
    episode_id: int
    include_specials: bool = False


class BulkBody(BaseModel):
    show_ids: list[int]
    action: str


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


@router.get("/auth/status")
def auth_status(request: Request) -> dict:
    return {"required": auth.enabled(), "authenticated": auth.authenticated(request)}


@router.post("/auth/login")
def login(body: LoginBody, response: Response) -> dict:
    if not auth.enabled():
        return {"authenticated": True}
    if not auth.check_password(body.password):
        raise HTTPException(status_code=401, detail="Wrong password")
    response.set_cookie(
        auth.COOKIE,
        auth.issue_token(),
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
    )
    return {"authenticated": True}


@router.post("/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE)
    return {"authenticated": False}


# --------------------------------------------------------------------------
# library
# --------------------------------------------------------------------------


@router.get("/home")
def get_home() -> dict:
    data = library.home()
    since = get_meta("last_seen_at")
    data["new_since_last_visit"] = len(library.new_since(since))
    data["last_refresh"] = refresh.last_refresh()
    # Surfaced here as well as in More: this warning means the library is about
    # to be lost, so it has to appear on the screen people actually open.
    data["storage_warning"] = storage.status()["warning"]
    return data


@router.get("/shows")
def list_shows(
    filter: str = Query("active", pattern="^(active|favorites|priority|unstarted|archived)$"),
    q: str = Query(""),
    sort: str = Query("name"),
) -> list[dict]:
    ids = library.followed_ids(include_archived=True)
    cards = [card for card in (library.show_card(sid) for sid in ids) if card]

    if filter == "archived":
        cards = [card for card in cards if card["archived"]]
    elif filter == "favorites":
        cards = [card for card in cards if card["favorite"]]
    elif filter == "priority":
        cards = [card for card in cards if card["priority"]]
    elif filter == "unstarted":
        cards = [card for card in cards if not card["archived"] and not card["started"]]
    else:
        cards = [card for card in cards if not card["archived"]]

    if q:
        needle = q.lower()
        cards = [card for card in cards if needle in (card["show"]["name"] or "").lower()]

    if sort == "progress":
        cards.sort(key=lambda c: c["progress"]["percent"], reverse=True)
    elif sort == "remaining":
        cards.sort(key=lambda c: c["progress"]["remaining"], reverse=True)
    elif sort == "recent":
        cards.sort(key=lambda c: (c["last_watched"] or {}).get("watched_at") or "", reverse=True)
    else:
        cards.sort(key=lambda c: (c["show"]["name"] or "").lower())
    return cards


@router.get("/shows/{show_id}")
def get_show(show_id: int) -> dict:
    detail = library.show_detail(show_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Show not in your library")
    return detail


@router.post("/shows")
async def add_show(body: AddShowBody) -> dict:
    try:
        return await library.add_show(body.tvmaze_id)
    except tvmaze.NotFound:
        raise HTTPException(status_code=404, detail="TVmaze has no show with that id")
    except tvmaze.TVmazeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.delete("/shows/{show_id}")
def remove_show(show_id: int, purge: bool = Query(False)) -> dict:
    library.unfollow_show(show_id, keep_history=not purge)
    return {"ok": True}


@router.post("/shows/{show_id}/archive")
def archive_show(show_id: int, body: FlagBody) -> dict:
    library.set_archived(show_id, body.value)
    return {"ok": True, "archived": body.value}


@router.post("/shows/{show_id}/favorite")
def favorite_show(show_id: int, body: FlagBody) -> dict:
    library.set_favorite(show_id, body.value)
    return {"ok": True, "favorite": body.value, "card": library.show_card(show_id)}


@router.post("/shows/{show_id}/priority")
def priority_show(show_id: int, body: FlagBody) -> dict:
    library.set_priority(show_id, body.value)
    return {"ok": True, "priority": body.value, "card": library.show_card(show_id)}


@router.post("/shows/bulk")
def bulk_shows(body: BulkBody) -> dict:
    try:
        result = library.bulk_update(body.show_ids, body.action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.post("/shows/{show_id}/refresh")
async def refresh_show(show_id: int) -> dict:
    try:
        await library.sync_show(show_id)
    except tvmaze.TVmazeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return library.show_detail(show_id) or {}


# --------------------------------------------------------------------------
# watching
# --------------------------------------------------------------------------


@router.post("/episodes/{episode_id}/watch")
def watch_episode(episode_id: int, body: WatchBody | None = None) -> dict:
    try:
        library.mark_watched(episode_id, (body.watched_at if body else None))
    except LookupError:
        raise HTTPException(status_code=404, detail="Unknown episode")
    return _episode_context(episode_id)


@router.delete("/episodes/{episode_id}/watch")
def unwatch_episode(episode_id: int) -> dict:
    library.unmark_watched(episode_id)
    return _episode_context(episode_id)


def _episode_context(episode_id: int) -> dict:
    row = connect().execute("SELECT show_id FROM episode WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown episode")
    return {"ok": True, "card": library.show_card(row["show_id"])}


@router.post("/shows/{show_id}/watch-through")
def watch_through(show_id: int, body: ThroughBody) -> dict:
    try:
        marked = library.mark_through(show_id, body.episode_id, body.include_specials)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "marked": marked, "card": library.show_card(show_id)}


@router.post("/shows/{show_id}/seasons/{season}/watch")
def watch_season(show_id: int, season: int, body: FlagBody) -> dict:
    changed = library.set_season_watched(show_id, season, body.value)
    return {"ok": True, "episodes": changed, "card": library.show_card(show_id)}


# --------------------------------------------------------------------------
# discovery, schedule, stats
# --------------------------------------------------------------------------


@router.get("/search")
async def search(q: str = Query(..., min_length=1)) -> list[dict]:
    try:
        return await library.search(q)
    except tvmaze.TVmazeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/upcoming")
def upcoming(days: int = Query(21, ge=1, le=120)) -> list[dict]:
    return library.upcoming(days)


@router.get("/calendar")
def calendar(
    back: int = Query(0, ge=0, le=400),
    forward: int = Query(35, ge=0, le=120),
) -> dict:
    return library.calendar(back_days=back, forward_days=forward)


@router.get("/new")
def new_episodes() -> dict:
    since = get_meta("last_seen_at")
    return {"since": since, "episodes": library.new_since(since)}


@router.post("/seen")
def mark_seen() -> dict:
    previous = library.touch_last_seen()
    return {"previous": previous}


@router.get("/history")
def get_history(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> list[dict]:
    return library.history(limit, offset)


@router.get("/stats")
def get_stats() -> dict:
    return library.stats()


@router.get("/status")
def get_status() -> dict:
    conn = connect()
    return {
        "following": conn.execute(
            "SELECT COUNT(*) AS n FROM follow WHERE archived = 0"
        ).fetchone()["n"],
        "archived": conn.execute(
            "SELECT COUNT(*) AS n FROM follow WHERE archived = 1"
        ).fetchone()["n"],
        "episodes_watched": conn.execute("SELECT COUNT(*) AS n FROM watch").fetchone()["n"],
        "last_refresh": refresh.last_refresh(),
        "refresh_interval_hours": config.REFRESH_INTERVAL_HOURS,
        "auth_required": auth.enabled(),
        "backup": backup.status(),
        "storage": storage.status(),
    }


@router.post("/refresh")
async def run_refresh(force: bool = Query(False)) -> dict:
    return await refresh.refresh_all(force=force)


# --------------------------------------------------------------------------
# import / export
# --------------------------------------------------------------------------


@router.post("/import")
async def import_export(
    file: UploadFile = File(...),
    dry_run: bool = Form(True),
    follow_shows: bool = Form(True),
) -> dict:
    """Start an import and return its job id; poll /api/import/{id} for progress."""
    suffix = Path(file.filename or "upload").suffix or ".zip"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        shutil.copyfileobj(file.file, handle)
        temp_path = Path(handle.name)
    job_id = jobs.start_import(
        temp_path, filename=file.filename, dry_run=dry_run, follow_shows=follow_shows
    )
    return {"job_id": job_id}


@router.get("/import/{job_id}")
def import_status(job_id: int) -> dict:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such import")
    return job


@router.get("/imports")
def import_history(limit: int = Query(10, ge=1, le=50)) -> list[dict]:
    rows = connect().execute(
        "SELECT id, created_at, filename, dry_run, status, stage, done, total, error, report "
        "FROM import_job ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        try:
            item["report"] = json.loads(item["report"] or "{}")
        except json.JSONDecodeError:
            item["report"] = {}
        output.append(item)
    return output


@router.get("/export")
def export_library() -> dict:
    """A portable backup: which shows you follow and every episode you watched."""
    return library.export_payload()


@router.get("/backups")
def backup_status() -> dict:
    return {**backup.status(), "files": backup.list_backups()}


@router.post("/backups")
async def run_backup(force: bool = Query(False)) -> dict:
    return await asyncio.to_thread(backup.write_backup, force)


@router.get("/backups/{name}")
def download_backup(name: str) -> FileResponse:
    """Serve one snapshot. The name is matched against the listing, never joined
    onto a path, so it cannot escape the backup directory."""
    if name not in {item["name"] for item in backup.list_backups()}:
        raise HTTPException(status_code=404, detail="No such backup")
    return FileResponse(
        config.BACKUP_DIR / name, media_type="application/json", filename=name
    )


@router.post("/restore")
async def restore_library(payload: dict) -> dict:
    # Must be async: starting the job needs the running event loop, which a
    # threadpool-dispatched sync endpoint does not have.
    """Rebuild the library from a backup. Runs as a job; poll /api/import/{id}.

    A backup names every show by its TVmaze id, so this is just an import that
    needs no matching — the slow part is re-fetching episode lists.
    """
    if payload.get("format") != importer.OWN_FORMAT:
        raise HTTPException(status_code=400, detail="Not a tv-tracker backup file")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as handle:
        json.dump(payload, handle)
        temp_path = Path(handle.name)

    job_id = jobs.start_import(
        temp_path, filename="restore.json", dry_run=False, follow_shows=True
    )
    return {"job_id": job_id}
