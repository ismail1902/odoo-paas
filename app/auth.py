"""Authentication, sessions and role checks.

Sessions are signed cookies (itsdangerous). Passwords use PBKDF2-SHA256,
stored as ``pbkdf2$<iterations>$<salt_hex>$<hash_hex>``. Only stdlib crypto
plus itsdangerous; no passlib/bcrypt dependency on purpose.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlmodel import Session

from app.config import settings
from app.models import ROLE_RANK, User

COOKIE_NAME = "odoo_paas_session"
TWOFA_COOKIE = "odoo_paas_2fa"
_PBKDF2_ITERATIONS = 260_000
_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="odoo-paas-session")
_twofa_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="odoo-paas-2fa")


# --- Passwords ---------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# --- Sessions ----------------------------------------------------------------


def login_user(response: Response, user: User) -> None:
    token = _serializer.dumps({"uid": user.id})
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.SESSION_MAX_AGE,
    )
    response.delete_cookie(TWOFA_COOKIE)


def logout_user(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie(TWOFA_COOKIE)


def set_pending_2fa(response: Response, user: User) -> None:
    token = _twofa_serializer.dumps({"uid": user.id})
    response.set_cookie(
        TWOFA_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=300,
    )


def pending_2fa_user(request: Request, session: Session) -> Optional[User]:
    token = request.cookies.get(TWOFA_COOKIE)
    if not token:
        return None
    try:
        data = _twofa_serializer.loads(token, max_age=300)
    except BadSignature:
        return None
    user_id = data.get("uid")
    if not user_id:
        return None
    return session.get(User, user_id)


def clear_pending_2fa(response: Response) -> None:
    response.delete_cookie(TWOFA_COOKIE)


def _user_from_token(token: Optional[str], session: Session) -> Optional[User]:
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=settings.SESSION_MAX_AGE)
    except BadSignature:
        return None
    user_id = data.get("uid")
    if not user_id:
        return None
    return session.get(User, user_id)


def current_user(request: Request, session: Session) -> Optional[User]:
    return _user_from_token(request.cookies.get(COOKIE_NAME), session)


def current_user_ws(cookies, session: Session) -> Optional[User]:
    """For WebSocket connections (Starlette exposes .cookies on them too)."""
    return _user_from_token(cookies.get(COOKIE_NAME), session)


# --- Roles -------------------------------------------------------------------

# Ranked roles (developer < tester < admin). Build-stage rules:
#   production shell  -> admin only
#   staging shell     -> tester or admin
#   development shell -> any role
STAGE_MIN_ROLE = {"production": "admin", "staging": "tester", "development": "developer"}


def has_role(user: Optional[User], minimum: str) -> bool:
    if user is None:
        return False
    return ROLE_RANK.get(user.role, -1) >= ROLE_RANK.get(minimum, 99)


def can_open_shell(user: Optional[User], stage: str) -> bool:
    return has_role(user, STAGE_MIN_ROLE.get(stage, "admin"))
