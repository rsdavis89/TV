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
