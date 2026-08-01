"""Optional single-password gate, for when the app is exposed beyond LAN."""

from __future__ import annotations

import base64
import hmac
import secrets
import time
from hashlib import sha256

from fastapi import Request

from . import config
from .db import get_meta, set_meta

COOKIE = "tv_session"

# A single shared password with no throttle is one long-running script away from
# being guessed. The counter is global rather than per-IP because there is only
# ever one legitimate user, because behind a platform proxy every request
# arrives from the same address, and because the forwarded-for header that would
# tell them apart is written by the client.
#
# The throttle is a growing delay rather than a lockout. A lockout is the
# obvious design and the wrong one: whoever is hammering the login decides when
# you can next get in, which hands an attacker a way to keep you out of your own
# app. A delay costs the attacker their guess rate and costs you a few seconds
# once, since a correct password clears the count.
FREE_ATTEMPTS = 2
MAX_DELAY_SECONDS = 10
# A backstop for a parallel attack, which delays alone would not slow: enough
# wrong answers in the window and nothing is accepted until it drains. Reaching
# it by hand is not plausible; reaching it with a script means being locked out
# is the least of the day's problems.
MAX_FAILURES = 60
FAILURE_WINDOW_SECONDS = 900

_failures: list[float] = []


def _recent_failures() -> list[float]:
    cutoff = time.monotonic() - FAILURE_WINDOW_SECONDS
    _failures[:] = [at for at in _failures if at > cutoff]
    return _failures


def locked_out() -> int:
    """Seconds until attempts are accepted again, or 0 when they are now."""
    recent = _recent_failures()
    if len(recent) < MAX_FAILURES:
        return 0
    return max(int(recent[0] + FAILURE_WINDOW_SECONDS - time.monotonic()) + 1, 1)


def failure_delay() -> float:
    """How long to wait before answering, given how badly this has been going."""
    count = len(_recent_failures())
    if count <= FREE_ATTEMPTS:
        return 0.0
    return float(min(2 ** (count - FREE_ATTEMPTS - 1), MAX_DELAY_SECONDS))


def record_failure() -> None:
    _recent_failures().append(time.monotonic())


def clear_failures() -> None:
    _failures.clear()


def enabled() -> bool:
    return bool(config.PASSWORD)


def _secret() -> bytes:
    if config.SECRET_KEY:
        return config.SECRET_KEY.encode()
    stored = get_meta("secret_key")
    if not stored:
        stored = secrets.token_hex(32)
        set_meta("secret_key", stored)
    return stored.encode()


def _sign(payload: str) -> str:
    digest = hmac.new(_secret(), payload.encode(), sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_token() -> str:
    expiry = str(int(time.time()) + config.SESSION_DAYS * 86400)
    return f"{expiry}.{_sign(expiry)}"


def valid_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expiry, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(expiry)):
        return False
    try:
        return int(expiry) > time.time()
    except ValueError:
        return False


def check_password(candidate: str) -> bool:
    return hmac.compare_digest(candidate or "", config.PASSWORD)


def authenticated(request: Request) -> bool:
    if not enabled():
        return True
    return valid_token(request.cookies.get(COOKIE))
