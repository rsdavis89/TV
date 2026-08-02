"""Thin async client for the TVmaze API.

TVmaze is free and needs no API key, but it asks for no more than 20 calls per
10 seconds, so every request goes through a small rate limiter.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from . import config


class TVmazeError(RuntimeError):
    pass


class NotFound(TVmazeError):
    pass


class _RateLimiter:
    """Sliding window limiter shared by every call in the process."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._hits = [t for t in self._hits if now - t < self.window]
                if len(self._hits) < self.limit:
                    self._hits.append(now)
                    return
                await asyncio.sleep(self.window - (now - self._hits[0]) + 0.05)


_limiter = _RateLimiter(config.TVMAZE_RATE, config.TVMAZE_RATE_WINDOW)
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.TVMAZE_BASE,
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,  # /lookup/shows answers with a 301
            headers={"User-Agent": "self-hosted-tv-tracker/1.0"},
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _get(path: str, params: dict[str, Any] | None = None, attempts: int = 4) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        await _limiter.acquire()
        try:
            response = await client().get(path, params=params)
        except httpx.HTTPError as exc:  # transport-level failure, worth a retry
            last_error = exc
            await asyncio.sleep(2**attempt)
            continue

        if response.status_code == 404:
            raise NotFound(f"TVmaze has nothing at {path}")
        if response.status_code == 429 or response.status_code >= 500:
            last_error = TVmazeError(f"TVmaze returned {response.status_code} for {path}")
            await asyncio.sleep(2**attempt)
            continue
        if response.status_code >= 400:
            raise TVmazeError(f"TVmaze returned {response.status_code} for {path}")

        if not response.content:
            return None
        return response.json()

    raise TVmazeError(f"TVmaze request failed after {attempts} attempts: {last_error}")


async def search_shows(query: str) -> list[dict]:
    """Full-text search. Returns the raw show payloads, best match first."""
    results = await _get("/search/shows", {"q": query}) or []
    return [item["show"] for item in results if item.get("show")]


async def get_show(show_id: int) -> dict:
    return await _get(f"/shows/{show_id}")


async def get_show_with_episodes(show_id: int) -> dict:
    """One call for the show, its full episode list and its cast."""
    return await _get(
        f"/shows/{show_id}", {"embed[]": ["episodes", "cast"], "specials": 1}
    )


async def get_cast(show_id: int) -> list[dict]:
    """Cast on its own, for shows already synced before it was being stored."""
    return await _get(f"/shows/{show_id}/cast") or []


async def get_episodes(show_id: int) -> list[dict]:
    return await _get(f"/shows/{show_id}/episodes", {"specials": 1}) or []


async def lookup_by_tvdb(tvdb_id: int) -> dict | None:
    """TV Time exports carry TVDB ids, which TVmaze can resolve directly."""
    try:
        return await _get("/lookup/shows", {"thetvdb": tvdb_id})
    except NotFound:
        return None


async def lookup_by_imdb(imdb_id: str) -> dict | None:
    try:
        return await _get("/lookup/shows", {"imdb": imdb_id})
    except NotFound:
        return None


async def full_schedule() -> list[dict]:
    """Every future episode TVmaze knows about, in one request.

    Around 10 MB, so it is deliberately not used for anything that runs often —
    but it replaces a request per day and covers an unlimited horizon.
    """
    await _limiter.acquire()
    response = await client().get("/schedule/full", timeout=httpx.Timeout(120.0))
    response.raise_for_status()
    return response.json()


async def updates_since(period: str = "week") -> dict[str, int]:
    """Map of show id -> last-updated epoch, for cheap staleness checks."""
    return await _get("/updates/shows", {"since": period}) or {}
