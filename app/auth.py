import contextvars
import secrets

from fastapi import Cookie, HTTPException
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings
from app.db import get_supabase

COOKIE_NAME = "session"
MAX_AGE = 60 * 60 * 24 * 7  # 7 days

_serializer = URLSafeTimedSerializer(settings.session_secret, salt="auth-session")

# Holds the decoded session payload for the lifetime of one request - set by
# current_user() (a dependency every protected route already runs), read by
# app.scoping's workspace-scoped lookups and the sidebar_clients()/
# current_user_email() Jinja globals, neither of which has direct access to
# the request/dependency-injection chain. Safe across FastAPI's threadpool
# for sync routes: Starlette runs each request via contextvars.copy_context(),
# so this never leaks between concurrent requests.
_session_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("session_ctx", default=None)


def sign_in(email: str, password: str) -> str:
    """Authenticate against Supabase Auth, return a signed session cookie value."""
    db = get_supabase()
    result = db.auth.sign_in_with_password({"email": email, "password": password})
    rows = db.table("workspace_members").select("workspace_id").eq("user_id", result.user.id).execute().data
    if not rows:
        raise ValueError("This account isn't linked to a workspace yet.")
    return _serializer.dumps({"email": email, "workspace_id": rows[0]["workspace_id"]})


def sign_up(email: str, password: str, workspace_name: str, invite_code: str) -> str:
    """Registers a new Supabase Auth user, then either joins an existing
    workspace (invite_code given, matched against workspaces.invite_code -
    see migrations/006_workspaces.sql) or creates a brand-new one. Returns a
    signed session cookie value, same contract as sign_in(), so a fresh
    registration logs straight in - our session is fully independent of
    Supabase's own auth session (sign_in() already discards that too), so
    this works even if the Supabase project has email confirmation enabled."""
    db = get_supabase()
    invite_code = invite_code.strip()

    workspace = None
    if invite_code:
        rows = db.table("workspaces").select("*").eq("invite_code", invite_code).execute().data
        if not rows:
            raise ValueError("That invite code doesn't match any workspace.")
        workspace = rows[0]

    result = db.auth.sign_up({"email": email, "password": password})
    user_id = result.user.id

    if workspace is None:
        name = workspace_name.strip() or f"{email.split('@')[0]}'s workspace"
        # ponytail: invite_code never expires or rotates - a leaked link
        # stays valid forever. Add a "regenerate invite code" action on
        # /settings if that ever needs to be revocable.
        workspace = db.table("workspaces").insert(
            {"name": name, "invite_code": secrets.token_urlsafe(9)}
        ).execute().data[0]

    # ponytail: role is informational only - every member has identical
    # permissions today, no owner-only action exists yet. Add real
    # permission checks if a workspace ever needs to restrict what a
    # member can do (e.g. only the owner can remove a teammate).
    db.table("workspace_members").insert(
        {
            "user_id": user_id,
            "workspace_id": workspace["id"],
            "email": email,
            "role": "member" if invite_code else "owner",
        }
    ).execute()

    return _serializer.dumps({"email": email, "workspace_id": workspace["id"]})


async def current_user(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict:
    """async on purpose, not just a sync function: FastAPI runs a sync
    dependency in its own threadpool thread via a fresh copy of the current
    context, so a contextvar.set() inside it never reaches the route
    handler's own (separately-copied) thread - verified live, the scoping
    checks below silently saw the default (None) workspace every time until
    this was made async, which runs inline in the request's own async
    context instead and avoids the threadpool split entirely."""
    if not session:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    try:
        payload = _serializer.loads(session, max_age=MAX_AGE)
    except BadSignature:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    _session_ctx.set(payload)
    return payload


def current_workspace_id() -> str | None:
    payload = _session_ctx.get()
    return payload.get("workspace_id") if payload else None


def current_user_email() -> str | None:
    payload = _session_ctx.get()
    return payload.get("email") if payload else None


class LoginRedirect(Exception):
    pass
