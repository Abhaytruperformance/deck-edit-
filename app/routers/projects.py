from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.ai.draft import DraftError, generate_draft
from app.auth import current_user
from app.claude_artifact import (
    ImportError_,
    fetch_artifact,
    make_slide_render_idempotent,
    parse_html_upload,
    upload_images,
)
from app.db import get_supabase, sidebar_clients
from app.input_parsing import parse_upload
from app.models.schemas import DeliverableType

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(current_user)])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["sidebar_clients"] = lambda: sidebar_clients(get_supabase())


def _latest_input(db, project_id: UUID) -> dict | None:
    rows = (
        db.table("inputs")
        .select("*")
        .eq("project_id", str(project_id))
        .order("version", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _get_artifact(db, project_id: UUID) -> dict | None:
    rows = db.table("artifacts").select("*").eq("project_id", str(project_id)).limit(1).execute().data
    return rows[0] if rows else None


def _insert_input(db, project_id: UUID, raw_data: dict, source_type: str) -> None:
    previous = _latest_input(db, project_id)
    next_version = (previous["version"] + 1) if previous else 1
    db.table("inputs").insert(
        {
            "project_id": str(project_id),
            "source_type": source_type,
            "raw_data": raw_data,
            "version": next_version,
        }
    ).execute()


def _persist_draft(db, project_id: UUID, blocks: list[dict]) -> None:
    existing = _get_artifact(db, project_id)
    if existing:
        db.table("artifacts").update({"blocks": blocks, "current_version_id": None}).eq(
            "id", existing["id"]
        ).execute()
    else:
        db.table("artifacts").insert(
            {"project_id": str(project_id), "blocks": blocks, "current_version_id": None}
        ).execute()


def _save_raw_html(db, project_id: UUID, raw_html: str) -> None:
    """Exact-clone edit-in-place mode: stores the imported/uploaded HTML
    verbatim instead of running it through generate_draft() into blocks -
    see app/claude_artifact.py's inject_edit_script(). Only meaningful for
    animated_html projects (a clone can't become a pptx/xlsx).

    Applies make_slide_render_idempotent() once, here, at upload time - a
    deck whose own boot script appends its slides into an existing
    container on load is only safe to run once; our "exact clone, edit in
    place" model stores the already-rendered DOM (script tag included), so
    without this the same script re-runs on every later view and duplicates
    (then crashes on) its own content. Doing it once here means it's baked
    into raw_html permanently and every other read path stays untouched."""
    raw_html = make_slide_render_idempotent(raw_html)
    existing = _get_artifact(db, project_id)
    if existing:
        db.table("artifacts").update({"raw_html": raw_html, "current_version_id": None}).eq(
            "id", existing["id"]
        ).execute()
    else:
        db.table("artifacts").insert(
            {"project_id": str(project_id), "raw_html": raw_html, "current_version_id": None}
        ).execute()


@router.get("/{project_id}")
def project_detail(request: Request, project_id: UUID):
    db = get_supabase()
    project = db.table("projects").select("*").eq("id", str(project_id)).single().execute().data
    client = db.table("clients").select("*").eq("id", project["client_id"]).single().execute().data
    latest_input = _latest_input(db, project_id)
    artifact = _get_artifact(db, project_id)
    return templates.TemplateResponse(
        request,
        "projects/detail.html",
        {
            "project": project,
            "client": client,
            "latest_input": latest_input,
            "artifact": artifact,
            "active": "draft",
            "has_artifact": artifact is not None,
        },
    )


@router.post("/{project_id}/update")
def update_project(project_id: UUID, title: str = Form(...), deliverable_type: DeliverableType = Form(...)):
    db = get_supabase()
    project = db.table("projects").select("client_id").eq("id", str(project_id)).execute().data
    if not project:
        raise HTTPException(404, "project not found")
    db.table("projects").update({"title": title, "deliverable_type": deliverable_type}).eq(
        "id", str(project_id)
    ).execute()
    return RedirectResponse(f"/clients/{project[0]['client_id']}", status_code=303)


@router.post("/{project_id}/delete")
def delete_project(project_id: UUID):
    """Cascades through inputs/artifacts/artifact_versions/shares via the FK
    constraints in migrations/001_init.sql - no manual cleanup needed."""
    db = get_supabase()
    project = db.table("projects").select("client_id").eq("id", str(project_id)).execute().data
    if not project:
        raise HTTPException(404, "project not found")
    db.table("projects").delete().eq("id", str(project_id)).execute()
    return RedirectResponse(f"/clients/{project[0]['client_id']}", status_code=303)


@router.get("/{project_id}/input")
def input_form(request: Request, project_id: UUID):
    db = get_supabase()
    project = db.table("projects").select("*").eq("id", str(project_id)).single().execute().data
    latest_input = _latest_input(db, project_id)
    return templates.TemplateResponse(
        request,
        "projects/input.html",
        {
            "project": project,
            "latest_input": latest_input,
            "active": "input",
            "has_artifact": _get_artifact(db, project_id) is not None,
        },
    )


@router.post("/{project_id}/input")
def save_input(
    project_id: UUID,
    business_name: str = Form(...),
    goals: str = Form(""),
    key_data_points: str = Form(""),
    notes: str = Form(""),
):
    db = get_supabase()
    raw_data = {
        "business_name": business_name,
        "goals": goals,
        "key_data_points": key_data_points,
        "notes": notes,
    }
    _insert_input(db, project_id, raw_data, source_type="form")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/input/upload")
async def upload_input(request: Request, project_id: UUID, file: UploadFile = File(...)):
    db = get_supabase()
    data = await file.read()
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""

    if ext in ("html", "htm"):
        project = db.table("projects").select("deliverable_type").eq("id", str(project_id)).single().execute().data
        if project["deliverable_type"] == "animated_html":
            # Exact clone: no block extraction at all, straight into the
            # edit-in-place raw-HTML editor.
            _save_raw_html(db, project_id, data.decode("utf-8", errors="replace"))
            return RedirectResponse(f"/projects/{project_id}/editor", status_code=303)

    source_type = "html_upload" if ext in ("html", "htm") else "file_upload"

    try:
        if source_type == "html_upload":
            raw_data, images = parse_html_upload(data)
            available_images = upload_images(db, images)
            if available_images:
                raw_data["available_images"] = available_images
        else:
            raw_data = parse_upload(file.filename, data)
    except ValueError as e:
        project = db.table("projects").select("*").eq("id", str(project_id)).single().execute().data
        return templates.TemplateResponse(
            request,
            "projects/input.html",
            {
                "project": project,
                "latest_input": _latest_input(db, project_id),
                "upload_error": str(e),
                "active": "input",
                "has_artifact": _get_artifact(db, project_id) is not None,
            },
            status_code=422,
        )
    _insert_input(db, project_id, raw_data, source_type=source_type)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/input/claude-artifact")
def import_claude_artifact(request: Request, project_id: UUID, url: str = Form(...)):
    """Phase 3 addition: import a public Claude artifact link as an input
    source, then immediately draft from it (same generate_draft() pipeline,
    same validate/retry/fail-closed invariant as the form/upload paths) and
    land the user in the canvas view rather than the bare form.

    For an animated_html project, skip block extraction entirely and store
    the content frame's own HTML as an exact clone instead - a pptx/xlsx
    project has nowhere to put arbitrary markup, so those still go through
    generate_draft() as before.
    """
    db = get_supabase()
    project = db.table("projects").select("*").eq("id", str(project_id)).single().execute().data

    try:
        raw_data, images, raw_html = fetch_artifact(url)
    except ImportError_ as e:
        return templates.TemplateResponse(
            request,
            "projects/input.html",
            {
                "project": project,
                "latest_input": _latest_input(db, project_id),
                "import_error": str(e),
                "active": "input",
                "has_artifact": _get_artifact(db, project_id) is not None,
            },
            status_code=422,
        )

    if project["deliverable_type"] == "animated_html" and raw_html:
        _save_raw_html(db, project_id, raw_html)
        return RedirectResponse(f"/projects/{project_id}/editor", status_code=303)

    available_images = upload_images(db, images)
    if available_images:
        raw_data["available_images"] = available_images
    _insert_input(db, project_id, raw_data, source_type="claude_artifact_url")

    try:
        blocks = generate_draft(raw_data, source_type="claude_artifact_url")
    except DraftError as e:
        client = db.table("clients").select("*").eq("id", project["client_id"]).single().execute().data
        existing_artifact = _get_artifact(db, project_id)
        return templates.TemplateResponse(
            request,
            "projects/detail.html",
            {
                "project": project,
                "client": client,
                "latest_input": _latest_input(db, project_id),
                "artifact": existing_artifact,
                "draft_error": str(e),
                "active": "draft",
                "has_artifact": existing_artifact is not None,
            },
            status_code=422,
        )

    _persist_draft(db, project_id, blocks)
    return RedirectResponse(f"/projects/{project_id}/editor?view=canvas", status_code=303)


@router.post("/{project_id}/draft")
def create_draft(request: Request, project_id: UUID):
    db = get_supabase()
    project = db.table("projects").select("*").eq("id", str(project_id)).single().execute().data
    client = db.table("clients").select("*").eq("id", project["client_id"]).single().execute().data
    latest_input = _latest_input(db, project_id)
    if latest_input is None:
        return RedirectResponse(f"/projects/{project_id}/input", status_code=303)

    try:
        blocks = generate_draft(latest_input["raw_data"], source_type=latest_input["source_type"])
    except DraftError as e:
        existing_artifact = _get_artifact(db, project_id)
        return templates.TemplateResponse(
            request,
            "projects/detail.html",
            {
                "project": project,
                "client": client,
                "latest_input": latest_input,
                "artifact": existing_artifact,
                "draft_error": str(e),
                "active": "draft",
                "has_artifact": existing_artifact is not None,
            },
            status_code=422,
        )

    _persist_draft(db, project_id, blocks)
    return RedirectResponse(f"/projects/{project_id}/editor", status_code=303)
