from fastapi import Cookie, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings
from app.db import get_supabase

COOKIE_NAME = "session"
MAX_AGE = 60 * 60 * 24 * 7  # 7 days

_serializer = URLSafeTimedSerializer(settings.session_secret, salt="auth-session")


def sign_in(email: str, password: str) -> str:
    """Authenticate against Supabase Auth, return a signed session cookie value."""
    get_supabase().auth.sign_in_with_password({"email": email, "password": password})
    return _serializer.dumps({"email": email})


def current_user(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict:
    if not session:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    try:
        return _serializer.loads(session, max_age=MAX_AGE)
    except BadSignature:
        raise HTTPException(status_code=303, headers={"Location": "/login"})


class LoginRedirect(Exception):
    pass
