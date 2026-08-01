"""Application entry point: wires the API, the static PWA and the refresh loop."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, backup, config, db, premieres, refresh, storage, tvmaze
from .api import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("tv")

OPEN_PATHS = {"/api/auth/status", "/api/auth/login", "/api/auth/logout"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.migrate()
    storage.record_start()
    tasks = [
        asyncio.create_task(refresh.scheduler()),
        asyncio.create_task(backup.scheduler()),
        asyncio.create_task(premieres.scheduler()),
    ]
    where = storage.status()
    log.info(
        "TV tracker ready; database at %s (%d bytes, created %s, start #%d)",
        where["database"],
        where["size_bytes"],
        where["created_at"],
        where["starts"],
    )
    if where["starts"] == 1:
        log.warning(
            "This database was created just now. If that happens on every "
            "deploy, storage is not persisting between restarts."
        )
    if where["warning"]:
        log.warning("STORAGE IS NOT PERSISTENT: %s", where["warning"])
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await tvmaze.close()


# The schema lives under /api so the gate below covers it. At FastAPI's default
# /openapi.json it sits outside the prefix and is readable without logging in.
app = FastAPI(
    title="TV Tracker",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)


@app.middleware("http")
async def gate(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") and path not in OPEN_PATHS and not auth.authenticated(request):
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return await call_next(request)


app.include_router(router)


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    # Served from the root so its cache scope covers the whole app.
    return FileResponse(config.WEB_DIR / "sw.js", media_type="application/javascript")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}


app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    run()
