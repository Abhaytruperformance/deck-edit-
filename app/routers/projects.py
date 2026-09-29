from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from app.ai.draft import DraftError, generate_draft
from app.auth import current_user, current_user_email, current_workspace_id
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
from app.scoping import get_project_or_404
from app.templates import templates

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(current_user)])
templates.env.globals["sidebar_clients"] = lambda: sidebar_clients(get_supabase())
templates.env.globals["current_user_email"] = current_user_email


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


def _draft_or_error(request: Request, db, project: dict, raw_data: dict, source_type: str, success_url: str):
    """Shared tail of every 'draft it now' path: generate, persist, land in
    the editor - or re-render the project page with the error and persist
    nothing (the fail-closed invariant in TECHNICAL.md)."""
    try:
        blocks = generate_draft(raw_data, source_type=source_type)
    except DraftError as e:
        client = db.table("clients").select("*").eq("id", project["client_id"]).single().execute().data
        existing_artifact = _get_artifact(db, project["id"])
        return templates.TemplateResponse(
            request,
            "projects/detail.html",
            {
                "project": project,
                "client": client,
                "latest_input": _latest_input(db, project["id"]),
                "artifact": existing_artifact,
                "draft_error": str(e),
                "active": "draft",
                "has_artifact": existing_artifact is not None,
            },
            status_code=422,
        )
    _persist_draft(db, project["id"], blocks)
    return RedirectResponse(success_url, status_code=303)


def _ingest_file(db, project: dict, filename: str, data: bytes) -> tuple[dict | None, str]:
    """Turn an uploaded file into an input row. Returns (raw_data, source_type),
    or (None, "raw_html") when an HTML deck was stored verbatim as an exact
    clone for an animated_html project. Raises ValueError on unparseable input."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("html", "htm") and project["deliverable_type"] == "animated_html":
        _save_raw_html(db, project["id"], data.decode("utf-8", errors="replace"))
        return None, "raw_html"

    source_type = "html_upload" if ext in ("html", "htm") else "file_upload"
    if source_type == "html_upload":
        raw_data, images = parse_html_upload(data)
        available_images = upload_images(db, images)
        if available_images:
            raw_data["available_images"] = available_images
    else:
        raw_data = parse_upload(filename, data)
    _insert_input(db, project["id"], raw_data, source_type=source_type)
    return raw_data, source_type


def _ingest_claude(db, project: dict, url: str) -> dict | None:
    """Same contract as _ingest_file for a public claude.ai artifact link.
    Raises ImportError_ when the page can't be fetched or has no content."""
    raw_data, images, raw_html = fetch_artifact(url)
    if project["deliverable_type"] == "animated_html" and raw_html:
        _save_raw_html(db, project["id"], raw_html)
        return None
    available_images = upload_images(db, images)
    if available_images:
        raw_data["available_images"] = available_images
    _insert_input(db, project["id"], raw_data, source_type="claude_artifact_url")
    return raw_data


def _brief_raw_data(business_name: str, goals: str, key_data_points: str, notes: str) -> dict:
    return {
        "business_name": business_name,
        "goals": goals,
        "key_data_points": key_data_points,
        "notes": notes,
    }


@router.get("/new")
def new_project_form(request: Request, client_id: str | None = None):
    """One page that replaces client page -> new project -> input -> draft:
    pick (or add) a client, name the project, give it input, and it lands
    in the editor with a draft already generated."""
    clients = (
        get_supabase().table("clients").select("id, name").eq("workspace_id", current_workspace_id())
        .order("name").execute().data
    )
    return templates.TemplateResponse(
        request,
        "projects/new.html",
        {"clients": clients, "selected_client_id": client_id or ""},
    )


@router.post("/new")
async def create_project_and_draft(
    request: Request,
    client_id: str = Form(""),
    new_client_name: str = Form(""),
    title: str = Form(...),
    deliverable_type: DeliverableType = Form("animated_html"),
    source: str = Form("upload"),
    business_name: str = Form(""),
    goals: str = Form(""),
    key_data_points: str = Form(""),
    notes: str = Form(""),
    url: str = Form(""),
    file: UploadFile | None = File(None),
):
    db = get_supabase()

    def error_page(message: str, status_code: int = 422):
        clients = db.table("clients").select("id, name").eq("workspace_id", current_workspace_id()).order("name").execute().data
        return templates.TemplateResponse(
            request,
            "projects/new.html",
            {
                "clients": clients,
                "selected_client_id": client_id,
                "error": message,
                "form": {
                    "new_client_name": new_client_name, "title": title, "deliverable_type": deliverable_type,
                    "source": source, "business_name": business_name, "goals": goals,
                    "key_data_points": key_data_points, "notes": notes, "url": url,
                },
            },
            status_code=status_code,
        )

    workspace_id = current_workspace_id()

    # Resolve the client: an existing one, or create it inline.
    if client_id == "__new__" or not client_id:
        name = new_client_name.strip()
        if not name:
            return error_page("Pick a client or enter a name for a new one.")
        client_id = db.table("clients").insert({"name": name, "workspace_id": workspace_id}).execute().data[0]["id"]
    elif not db.table("clients").select("id").eq("id", client_id).eq("workspace_id", workspace_id).execute().data:
        return error_page("That client no longer exists.")

    project = (
        db.table("projects")
        .insert({
            "client_id": str(client_id), "title": title.strip(), "deliverable_type": deliverable_type,
            "workspace_id": workspace_id,
        })
        .execute()
        .data[0]
    )
    pid = project["id"]

    if source == "upload":
        if file is None or not file.filename:
            return RedirectResponse(f"/projects/{pid}/input", status_code=303)
        data = await file.read()
        try:
            raw_data, source_type = _ingest_file(db, project, file.filename, data)
        except ValueError as e:
            return RedirectResponse(f"/projects/{pid}/input", status_code=303) if not str(e) else error_page(str(e))
        if raw_data is None:
            return RedirectResponse(f"/projects/{pid}/editor", status_code=303)
        return _draft_or_error(request, db, project, raw_data, source_type, f"/projects/{pid}/editor")

    if source == "link":
        if not url.strip():
            return RedirectResponse(f"/projects/{pid}/input", status_code=303)
        try:
            raw_data = _ingest_claude(db, project, url.strip())
        except ImportError_ as e:
            return error_page(str(e))
        if raw_data is None:
            return RedirectResponse(f"/projects/{pid}/editor", status_code=303)
        return _draft_or_error(
            request, db, project, raw_data, "claude_artifact_url", f"/projects/{pid}/editor?view=canvas"
        )

    # Brief. An empty brief just creates the project and stops on its page;
    # anything filled in is saved and drafted straight away.
    if not any(v.strip() for v in (business_name, goals, key_data_points, notes)):
        return RedirectResponse(f"/projects/{pid}", status_code=303)
    client = db.table("clients").select("name").eq("id", str(client_id)).single().execute().data
    raw_data = _brief_raw_data(business_name.strip() or client["name"], goals, key_data_points, notes)
    _insert_input(db, pid, raw_data, source_type="form")
    return _draft_or_error(request, db, project, raw_data, "form", f"/projects/{pid}/editor")


@router.get("/{project_id}")
def project_detail(request: Request, project_id: UUID):
    db = get_supabase()
    project = get_project_or_404(db, project_id)
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
def update_project(project_id: UUID, title: str = Form(...), deliverable_type: DeliverableType | None = Form(None)):
    db = get_supabase()
    project = get_project_or_404(db, project_id)
    changes = {"title": title}
    if deliverable_type:
        changes["deliverable_type"] = deliverable_type
    db.table("projects").update(changes).eq(
        "id", str(project_id)
    ).execute()
    return RedirectResponse(f"/clients/{project['client_id']}", status_code=303)


@router.post("/{project_id}/delete")
def delete_project(project_id: UUID):
    """Cascades through inputs/artifacts/artifact_versions/shares via the FK
    constraints in migrations/001_init.sql - no manual cleanup needed."""
    db = get_supabase()
    project = get_project_or_404(db, project_id)
    db.table("projects").delete().eq("id", str(project_id)).execute()
    return RedirectResponse(f"/clients/{project['client_id']}", status_code=303)


@router.get("/{project_id}/input")
def input_form(request: Request, project_id: UUID):
    db = get_supabase()
    project = get_project_or_404(db, project_id)
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
    request: Request,
    project_id: UUID,
    business_name: str = Form(...),
    goals: str = Form(""),
    key_data_points: str = Form(""),
    notes: str = Form(""),
    then: str = Form(""),
):
    db = get_supabase()
    project = get_project_or_404(db, project_id)
    raw_data = _brief_raw_data(business_name, goals, key_data_points, notes)
    _insert_input(db, project_id, raw_data, source_type="form")
    if then == "draft":
        return _draft_or_error(request, db, project, raw_data, "form", f"/projects/{project_id}/editor")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/{project_id}/input/upload")
async def upload_input(request: Request, project_id: UUID, file: UploadFile = File(...)):
    """Parse the upload into an input row and draft from it straight away,
    so one click gets from file to editor. An HTML deck for an animated_html
    project is stored verbatim instead (exact clone, edit in place)."""
    db = get_supabase()
    project = get_project_or_404(db, project_id)
    data = await file.read()
    try:
        raw_data, source_type = _ingest_file(db, project, file.filename, data)
    except ValueError as e:
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
    if raw_data is None:
        return RedirectResponse(f"/projects/{project_id}/editor", status_code=303)
    return _draft_or_error(request, db, project, raw_data, source_type, f"/projects/{project_id}/editor")


@router.post("/{project_id}/input/claude-artifact")
def import_claude_artifact(request: Request, project_id: UUID, url: str = Form(...)):
    """Import a public Claude artifact link as an input source, then draft
    from it immediately (same generate_draft() pipeline and fail-closed
    invariant as the form/upload paths) and land in the canvas view. For an
    animated_html project the page is stored as an exact clone instead."""
    db = get_supabase()
    project = get_project_or_404(db, project_id)
    try:
        raw_data = _ingest_claude(db, project, url)
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
    if raw_data is None:
        return RedirectResponse(f"/projects/{project_id}/editor", status_code=303)
    return _draft_or_error(
        request, db, project, raw_data, "claude_artifact_url", f"/projects/{project_id}/editor?view=canvas"
    )


@router.post("/{project_id}/draft")
def create_draft(request: Request, project_id: UUID):
    db = get_supabase()
    project = get_project_or_404(db, project_id)
    latest_input = _latest_input(db, project_id)
    if latest_input is None:
        return RedirectResponse(f"/projects/{project_id}/input", status_code=303)
    return _draft_or_error(
        request, db, project, latest_input["raw_data"], latest_input["source_type"], f"/projects/{project_id}/editor"
    )
