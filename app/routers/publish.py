"""Phase 4/6/7: publish flow, public share route, and the three renderers'
common entry point. The public route never reads the live draft - only the
version pinned by `shares.published_version_id`.
"""
import secrets
import string
import time
from collections import defaultdict
from uuid import UUID

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.auth import current_user
from app.config import settings
from app.db import get_supabase
from app.renderers import html as html_renderer
from app.renderers import pptx as pptx_renderer
from app.renderers import xlsx as xlsx_renderer
from app.routers.editor import _create_version, _get_artifact_or_404

router = APIRouter(tags=["publish"])
templates = Jinja2Templates(directory="app/templates")

_share_serializer = URLSafeTimedSerializer(settings.session_secret, salt="share-access")
_SHARE_COOKIE_MAX_AGE = 60 * 60 * 24  # 1 day
_SLUG_ALPHABET = string.ascii_lowercase + string.digits

# ponytail: in-process rate limit, single-worker only - resets on restart and
# doesn't share state across processes. Move to Redis/DB if this ever runs
# with >1 worker or needs to survive restarts.
_password_attempts: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_ATTEMPTS = 10


def _generate_slug(db) -> str:
    for _ in range(10):
        slug = "".join(secrets.choice(_SLUG_ALPHABET) for _ in range(8))
        if not db.table("shares").select("id").eq("slug", slug).execute().data:
            return slug
    raise RuntimeError("could not generate a unique slug")


def _resolve_version_id(db, artifact: dict) -> str:
    """Whatever "publish the current draft" means: reuse the version already
    pinned by current_version_id if nothing's changed since the last save,
    otherwise snapshot one first - the same rule Save Version itself uses."""
    if artifact.get("current_version_id"):
        return artifact["current_version_id"]
    return _create_version(db, artifact)["id"]


@router.post("/projects/{project_id}/publish", dependencies=[Depends(current_user)])
def publish(project_id: UUID, label: str = Form(...), password: str = Form("")):
    """Always creates a NEW share - a project can have several independent
    published links live at once (one per client), each pinned to whatever
    version it was published with and carrying its own password, distinct
    from every other link on the same project. Republishing an *existing*
    link (pushing a newer version to a client who already has a link) is a
    separate action - see republish_share below - specifically so it never
    accidentally overwrites a different client's link."""
    db = get_supabase()
    artifact = _get_artifact_or_404(db, project_id)
    version_id = _resolve_version_id(db, artifact)

    insert = {
        "artifact_id": artifact["id"],
        "published_version_id": version_id,
        "slug": _generate_slug(db),
        "label": label,
    }
    if password:
        insert["password_hash"] = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db.table("shares").insert(insert).execute()

    db.table("projects").update({"status": "published"}).eq("id", str(project_id)).execute()
    return RedirectResponse(f"/projects/{project_id}/editor", status_code=303)


def _get_share_for_project_or_404(db, project_id: UUID, share_id: UUID) -> dict:
    """Scopes a share lookup to the project it claims to belong to, so one
    project's edit/delete form can't be pointed at another project's share
    id (e.g. a stale form re-submitted after switching projects)."""
    artifact = _get_artifact_or_404(db, project_id)
    rows = db.table("shares").select("*").eq("id", str(share_id)).eq("artifact_id", artifact["id"]).execute().data
    if not rows:
        raise HTTPException(404, "share not found")
    return rows[0]


@router.post("/projects/{project_id}/shares/{share_id}/republish", dependencies=[Depends(current_user)])
def republish_share(project_id: UUID, share_id: UUID, password: str = Form("")):
    """Points ONE existing link at the current draft's latest version,
    leaving every other share on this project (other clients' links)
    completely untouched. An empty password field keeps whatever password
    (or lack of one) the link already had - only a non-empty value changes it."""
    db = get_supabase()
    share = _get_share_for_project_or_404(db, project_id, share_id)
    artifact = _get_artifact_or_404(db, project_id)
    update = {"published_version_id": _resolve_version_id(db, artifact)}
    if password:
        update["password_hash"] = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db.table("shares").update(update).eq("id", share["id"]).execute()
    return RedirectResponse(f"/projects/{project_id}/editor", status_code=303)


@router.post("/projects/{project_id}/shares/{share_id}/delete", dependencies=[Depends(current_user)])
def delete_share(project_id: UUID, share_id: UUID):
    """Revokes one client's link without touching any other share on this
    project. project.status is left at "published" even if this was the
    last remaining share - status is sticky by design (see TECHNICAL.md),
    and deleting a link is not the same as un-publishing the project."""
    db = get_supabase()
    share = _get_share_for_project_or_404(db, project_id, share_id)
    db.table("shares").delete().eq("id", share["id"]).execute()
    return RedirectResponse(f"/projects/{project_id}/editor", status_code=303)


def _render_bytes(deliverable_type: str, blocks: list[dict]):
    if deliverable_type == "pptx":
        return pptx_renderer.render(blocks), "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"
    if deliverable_type == "xlsx":
        return xlsx_renderer.render(blocks), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    raise HTTPException(400, "animated_html has no file download - view it at the share link")


def _get_share_or_404(db, slug: str) -> dict:
    rows = db.table("shares").select("*").eq("slug", slug).execute().data
    if not rows:
        raise HTTPException(404, "share not found")
    return rows[0]


def _is_expired(share: dict) -> bool:
    if not share.get("expires_at"):
        return False
    from datetime import datetime, timezone

    expires_at = datetime.fromisoformat(share["expires_at"].replace("Z", "+00:00"))
    return datetime.now(timezone.utc) > expires_at


def _has_valid_session(request: Request, slug: str) -> bool:
    cookie = request.cookies.get(f"share_{slug}")
    if not cookie:
        return False
    try:
        data = _share_serializer.loads(cookie, max_age=_SHARE_COOKIE_MAX_AGE)
        return data.get("slug") == slug
    except BadSignature:
        return False


def _published_version(db, share: dict) -> dict:
    """Full pinned version row (blocks + raw_html) - each renderer filters
    blocks to enabled ones itself, per the renderer contract; raw_html, when
    set, is served/downloaded verbatim instead (exact-clone artifacts)."""
    return db.table("artifact_versions").select("*").eq("id", share["published_version_id"]).single().execute().data


@router.get("/s/{slug}")
def view_share(request: Request, slug: str):
    db = get_supabase()
    share = _get_share_or_404(db, slug)
    if _is_expired(share):
        return templates.TemplateResponse(request, "share/expired.html", status_code=410)

    if share.get("password_hash") and not _has_valid_session(request, slug):
        return templates.TemplateResponse(request, "share/password.html", {"slug": slug})

    db.table("shares").update({"view_count": share["view_count"] + 1}).eq("id", share["id"]).execute()

    artifact = db.table("artifacts").select("project_id").eq("id", share["artifact_id"]).single().execute().data
    project = db.table("projects").select("*").eq("id", artifact["project_id"]).single().execute().data
    version = _published_version(db, share)

    if version.get("raw_html"):
        return Response(version["raw_html"], media_type="text/html")

    if project["deliverable_type"] == "animated_html":
        return Response(html_renderer.render(version["blocks"]), media_type="text/html")

    return templates.TemplateResponse(
        request,
        "share/download.html",
        {"slug": slug, "project": project, "view_count": share["view_count"] + 1},
    )


@router.post("/s/{slug}/password")
def check_share_password(request: Request, slug: str, password: str = Form(...)):
    now = time.monotonic()
    attempts = _password_attempts[slug]
    attempts[:] = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW_SECONDS]
    if len(attempts) >= _RATE_LIMIT_MAX_ATTEMPTS:
        return templates.TemplateResponse(
            request,
            "share/password.html",
            {"slug": slug, "error": "Too many attempts - try again in a minute."},
            status_code=429,
        )
    attempts.append(now)

    db = get_supabase()
    share = _get_share_or_404(db, slug)
    if not share.get("password_hash") or not bcrypt.checkpw(password.encode(), share["password_hash"].encode()):
        return templates.TemplateResponse(
            request, "share/password.html", {"slug": slug, "error": "Incorrect password"}, status_code=401
        )

    response = RedirectResponse(f"/s/{slug}", status_code=303)
    cookie_value = _share_serializer.dumps({"slug": slug})
    response.set_cookie(f"share_{slug}", cookie_value, max_age=_SHARE_COOKIE_MAX_AGE, httponly=True, samesite="lax")
    return response


@router.get("/s/{slug}/download")
def download_share(request: Request, slug: str):
    db = get_supabase()
    share = _get_share_or_404(db, slug)
    if _is_expired(share):
        raise HTTPException(410, "This share link has expired")
    if share.get("password_hash") and not _has_valid_session(request, slug):
        raise HTTPException(403, "Password required")

    artifact = db.table("artifacts").select("project_id").eq("id", share["artifact_id"]).single().execute().data
    project = db.table("projects").select("*").eq("id", artifact["project_id"]).single().execute().data
    version = _published_version(db, share)
    if version.get("raw_html"):
        content, media_type, ext = version["raw_html"], "text/html", "html"
    else:
        content, media_type, ext = _render_bytes(project["deliverable_type"], version["blocks"])

    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{project["title"]}.{ext}"'},
    )
