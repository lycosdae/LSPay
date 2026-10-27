import hashlib
import hmac
import secrets
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import models as m
from ..db import get_db

_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


class LoginRequired(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> m.AdminUser:
    uid = request.session.get("uid")
    user = db.get(m.AdminUser, uid) if uid else None
    if user is None or not user.enabled:
        request.session.clear()
        raise LoginRequired()
    return user


def require(*roles: str):
    def dep(user: m.AdminUser = Depends(current_user)) -> m.AdminUser:
        if user.role not in roles:
            raise HTTPException(403, "沒有權限")
        return user

    return dep


require_admin = require(m.ROLE_ADMIN)
require_reviewer = require(m.ROLE_ADMIN, m.ROLE_REVIEWER)


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def check_csrf(request: Request) -> None:
    form = await request.form()
    sent = form.get("csrf") or ""
    expected = request.session.get("csrf") or ""
    if not expected or not hmac.compare_digest(str(sent), expected):
        raise HTTPException(400, "表單已過期，請重新整理頁面")


# Very small in-memory throttle: 5 failed logins per username+IP per 10 minutes.
_failures: dict[str, list[float]] = {}
WINDOW, LIMIT = 600, 5


def login_blocked(key: str) -> bool:
    now = time.time()
    hits = [t for t in _failures.get(key, []) if now - t < WINDOW]
    _failures[key] = hits
    return len(hits) >= LIMIT


def record_failure(key: str) -> None:
    _failures.setdefault(key, []).append(time.time())


def clear_failures(key: Optional[str]) -> None:
    _failures.pop(key or "", None)
