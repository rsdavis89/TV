"""HTTP API consumed by the web front end."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel

from . import auth, config, jobs, library, refresh, tvmaze
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
    return data


@router.get("/shows")
def list_shows(
    archived: bool = Query(False),
    q: str = Query(""),
    sort: str = Query("name"),
) -> list[dict]:
    ids = library.followed_ids(include_archived=True)
    cards = [card for card in (library.show_card(sid) for sid in ids) if card]
    cards = [card for card in cards if card["archived"] == archived]
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
    return {"ok": True, "favorite": body.value}


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
    conn = connect()
    follows = conn.execute(
        """
        SELECT s.id AS tvmaze_id, s.name, s.tvdb_id, s.imdb_id,
               f.followed_at, f.archived, f.favorite
        FROM follow f JOIN show s ON s.id = f.show_id ORDER BY s.name
        """
    ).fetchall()
    watches = conn.execute(
        """
        SELECT s.id AS tvmaze_id, s.name AS show_name, s.tvdb_id,
               e.season, e.number, e.name AS episode_name, w.watched_at, w.source
        FROM watch w
        JOIN episode e ON e.id = w.episode_id
        JOIN show s ON s.id = w.show_id
        ORDER BY s.name, e.season, e.number
        """
    ).fetchall()
    return {
        "format": "tv-tracker-export",
        "version": 1,
        "exported_at": utcnow(),
        "follows": [dict(row) for row in follows],
        "watches": [dict(row) for row in watches],
    }


@router.post("/restore")
async def restore_library(payload: dict) -> dict:
    """Load a backup produced by /api/export back into an empty (or partial) library."""
    if payload.get("format") != "tv-tracker-export":
        raise HTTPException(status_code=400, detail="Not a tv-tracker export file")

    wanted = {int(item["tvmaze_id"]) for item in payload.get("follows", []) if item.get("tvmaze_id")}
    wanted |= {int(item["tvmaze_id"]) for item in payload.get("watches", []) if item.get("tvmaze_id")}

    synced, failed = 0, []
    for show_id in sorted(wanted):
        try:
            await library.sync_show(show_id)
            synced += 1
        except tvmaze.TVmazeError:
            failed.append(show_id)

    with tx() as conn:
        conn.executemany(
            "INSERT INTO follow (show_id, followed_at, archived, favorite) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(show_id) DO NOTHING",
            [
                (
                    int(item["tvmaze_id"]),
                    item.get("followed_at") or utcnow(),
                    int(item.get("archived") or 0),
                    int(item.get("favorite") or 0),
                )
                for item in payload.get("follows", [])
                if item.get("tvmaze_id") and int(item["tvmaze_id"]) not in failed
            ],
        )

    marked = 0
    for item in payload.get("watches", []):
        row = connect().execute(
            "SELECT id FROM episode WHERE show_id = ? AND season = ? AND number = ?",
            (item.get("tvmaze_id"), item.get("season"), item.get("number")),
        ).fetchone()
        if row is None:
            continue
        library.mark_watched(row["id"], item.get("watched_at"), source="restore")
        marked += 1

    return {"shows_synced": synced, "shows_failed": failed, "episodes_marked": marked}
