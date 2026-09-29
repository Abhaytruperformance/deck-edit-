"""Phase 3: the block editor. All edits operate on the live draft
(`artifacts.blocks`) and immediately clear `current_version_id` per the
Save Version vs. Publish rule in TECHNICAL.md.
"""
import json
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import TypeAdapter, ValidationError

from app.auth import current_user
from app.claude_artifact import (
    dedupe_slides_if_needed,
    inject_edit_script,
    strip_hidden_slides,
    strip_native_authoring_chrome,
)
from app.db import get_supabase, sidebar_clients
from app.models.blocks import Block
from app.renderers import html as html_renderer
from app.renderers import pptx as pptx_renderer
from app.renderers import xlsx as xlsx_renderer

router = APIRouter(prefix="/projects/{project_id}", tags=["editor"], dependencies=[Depends(current_user)])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["sidebar_clients"] = lambda: sidebar_clients(get_supabase())

_block_adapter = TypeAdapter(Block)

# Minimal-but-valid content for a freshly added block of each type - the user
# fills it in immediately after via the same structured edit form as any
# other block, so these just need to pass Block validation, not look good.
_DEFAULT_CONTENT = {
    "title_slide": {"headline": "New headline", "subhead": ""},
    "text_block": {"heading": "New heading", "body": ""},
    "bullet_list": {"heading": "New heading", "items": []},
    "kpi_grid": {"title": "New metrics", "items": []},
    "comparison_table": {"headers": [], "rows": []},
    "chart": {"chart_type": "bar", "title": "New chart", "labels": [], "series": []},
    "image_block": {"caption": "", "image_ref": ""},
}


def _get_artifact_or_404(db, project_id: UUID) -> dict:
    rows = db.table("artifacts").select("*").eq("project_id", str(project_id)).limit(1).execute().data
    if not rows:
        raise HTTPException(404, "No artifact for this project yet - generate a draft first")
    return rows[0]


def _save_blocks(db, artifact_id: str, blocks: list[dict]) -> None:
    db.table("artifacts").update({"blocks": blocks, "current_version_id": None}).eq("id", artifact_id).execute()


def _blocks_response(request: Request, project_id: UUID, artifact: dict, error: str | None = None):
    # Every content/toggle/move/add/delete endpoint re-renders through here, so
    # picking the partial off the same "editor_view" cookie the page itself
    # reads means the canvas view and the form view each keep getting swapped
    # with their own markup, with zero extra plumbing per route.
    template = "projects/_canvas.html" if request.cookies.get("editor_view") == "canvas" else "projects/_blocks_list.html"
    return templates.TemplateResponse(
        request,
        template,
        {"project_id": project_id, "artifact": artifact, "error": error},
    )


def _parse_content_form(block_type: str, form) -> dict:
    """Decode the per-type edit form into a content dict matching that block's schema."""
    if block_type == "title_slide":
        return {"headline": form.get("headline", ""), "subhead": form.get("subhead", "")}

    if block_type == "text_block":
        return {"heading": form.get("heading", ""), "body": form.get("body", "")}

    if block_type == "bullet_list":
        items = [v.strip() for v in form.getlist("item") if v.strip()]
        return {"heading": form.get("heading", ""), "items": items}

    if block_type == "kpi_grid":
        labels = form.getlist("kpi_label")
        values = form.getlist("kpi_value")
        units = form.getlist("kpi_unit")
        changes = form.getlist("kpi_change_pct")
        items = []
        for label, value, unit, change in zip(labels, values, units, changes):
            if not label.strip():
                continue
            items.append(
                {
                    "label": label.strip(),
                    "value": float(value) if value.strip() else 0.0,
                    "unit": unit.strip(),
                    "change_pct": float(change) if change.strip() else None,
                }
            )
        return {"title": form.get("title", ""), "items": items}

    if block_type == "comparison_table":
        grid = json.loads(form.get("table_json", "{}") or "{}")
        headers = [h.strip() for h in grid.get("headers", [])]
        rows = [[str(c).strip() for c in row] for row in grid.get("rows", []) if any(str(c).strip() for c in row)]
        return {"headers": headers, "rows": rows}

    if block_type == "chart":
        grid = json.loads(form.get("chart_json", "{}") or "{}")
        labels = [l.strip() for l in grid.get("labels", [])]
        series = [
            {"name": s.get("name", "").strip(), "values": [float(v) for v in s.get("values", [])]}
            for s in grid.get("series", [])
            if s.get("name", "").strip()
        ]
        return {
            "chart_type": form.get("chart_type", "bar"),
            "title": form.get("title", ""),
            "labels": labels,
            "series": series,
        }

    if block_type == "image_block":
        return {"caption": form.get("caption", ""), "image_ref": form.get("image_ref", "")}

    raise ValueError(f"unknown block type: {block_type}")


@router.get("/editor")
def editor_page(request: Request, project_id: UUID, view: str | None = None):
    db = get_supabase()
    project = db.table("projects").select("*").eq("id", str(project_id)).single().execute().data
    artifact = _get_artifact_or_404(db, project_id)

    # ?view=... (an explicit toggle click, or a redirect from importing a
    # Claude artifact) always wins and is remembered; otherwise fall back to
    # whichever view this browser last used, defaulting to the form.
    requested_view = view if view in ("form", "canvas") else None
    editor_view = requested_view or request.cookies.get("editor_view", "form")

    response = templates.TemplateResponse(
        request,
        "projects/editor.html",
        {
            "project": project,
            "artifact": artifact,
            "active": "edit",
            "has_artifact": True,
            "editor_view": editor_view,
        },
    )
    if requested_view:
        response.set_cookie("editor_view", requested_view, max_age=60 * 60 * 24 * 365)
    return response


@router.get("/preview")
def preview_draft(project_id: UUID):
    """Content preview for the editor's live pane - always rendered through the
    HTML renderer regardless of deliverable_type, since it's the only renderer
    that's meaningful to look at in a browser. Not a pixel-accurate stand-in for
    the pptx/xlsx output, just a fast way to see structure and content."""
    db = get_supabase()
    artifact = _get_artifact_or_404(db, project_id)
    return Response(html_renderer.render(artifact["blocks"]), media_type="text/html")


@router.get("/raw-editor")
def raw_editor_frame(project_id: UUID):
    """The exact-clone edit-in-place surface: the stored raw_html served back
    with the contenteditable/autosave script from claude_artifact.py injected -
    edits are made directly on the original markup/CSS, not through a form."""
    db = get_supabase()
    artifact = _get_artifact_or_404(db, project_id)
    raw_html = artifact.get("raw_html")
    if not raw_html:
        raise HTTPException(404, "this project has no raw HTML clone")
    return Response(inject_edit_script(raw_html), media_type="text/html")


@router.post("/raw-editor/save")
async def save_raw_editor(project_id: UUID, request: Request):
    db = get_supabase()
    artifact = _get_artifact_or_404(db, project_id)
    body = await request.json()
    html = dedupe_slides_if_needed(body.get("html", ""))
    db.table("artifacts").update({"raw_html": html, "current_version_id": None}).eq("id", artifact["id"]).execute()
    return {"ok": True}


_ASSET_BUCKET = "deliverable-assets"


@router.post("/raw-editor/upload-image")
async def upload_raw_editor_image(project_id: UUID, file: UploadFile = File(...)):
    """Backs the raw-editor's "click an image to replace it" - unlike the
    block editor's image_block (which stores a Storage path and resolves it
    per-renderer), raw_html is served verbatim, so this writes the public
    URL straight into the <img src> and there's nothing else to resolve."""
    db = get_supabase()
    _get_artifact_or_404(db, project_id)
    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(400, "file must be an image")
    data = await file.read()
    ext = (content_type.split("/")[-1].split("+")[0] or "png")[:5]
    path = f"raw-editor/{project_id}/{uuid.uuid4().hex}.{ext}"
    bucket = db.storage.from_(_ASSET_BUCKET)
    bucket.upload(path, data, {"content-type": content_type})
    return {"url": bucket.get_public_url(path)}


_EXPORT_FORMATS = {
    "html": ("text/html", "html"),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
}


@router.get("/export")
def export_draft(project_id: UUID, format: str = "html"):
    """Download the live draft directly, without publishing first - HTML is
    always offered (a standalone page, same renderer as the preview pane)
    regardless of deliverable_type, since that's the one format every draft
    can produce; pptx/xlsx are offered when that's the project's own type."""
    if format not in _EXPORT_FORMATS:
        raise HTTPException(400, f"unknown export format: {format}")

    db = get_supabase()
    project = db.table("projects").select("title").eq("id", str(project_id)).single().execute().data
    artifact = _get_artifact_or_404(db, project_id)

    if artifact.get("raw_html") and format == "html":
        content = strip_native_authoring_chrome(strip_hidden_slides(artifact["raw_html"]))
    elif format == "pptx":
        content = pptx_renderer.render(artifact["blocks"])
    elif format == "xlsx":
        content = xlsx_renderer.render(artifact["blocks"])
    else:
        content = html_renderer.render(artifact["blocks"])

    media_type, ext = _EXPORT_FORMATS[format]
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{project["title"]}.{ext}"'},
    )


@router.post("/blocks/{block_id}/content")
async def update_block_content(request: Request, project_id: UUID, block_id: str):
    db = get_supabase()
    artifact = _get_artifact_or_404(db, project_id)
    form = await request.form()
    blocks = artifact["blocks"]

    index = next((i for i, b in enumerate(blocks) if b["id"] == block_id), None)
    if index is None:
        raise HTTPException(404, "block not found")

    try:
        content = _parse_content_form(blocks[index]["type"], form)
        updated_block = {**blocks[index], "content": content}
        _block_adapter.validate_python(updated_block)  # fail fast on a malformed edit
    except (ValueError, ValidationError) as e:
        return _blocks_response(request, project_id, artifact, error=f"Invalid edit for {block_id}: {e}")

    blocks[index] = updated_block
    _save_blocks(db, artifact["id"], blocks)
    artifact["blocks"] = blocks
    artifact["current_version_id"] = None
    return _blocks_response(request, project_id, artifact)


@router.post("/blocks/{block_id}/toggle")
def toggle_block(request: Request, project_id: UUID, block_id: str):
    db = get_supabase()
    artifact = _get_artifact_or_404(db, project_id)
    blocks = artifact["blocks"]
    for b in blocks:
        if b["id"] == block_id:
            b["enabled"] = not b["enabled"]
            break
    else:
        raise HTTPException(404, "block not found")

    _save_blocks(db, artifact["id"], blocks)
    artifact["blocks"] = blocks
    artifact["current_version_id"] = None
    return _blocks_response(request, project_id, artifact)


@router.post("/blocks/{block_id}/move")
def move_block(request: Request, project_id: UUID, block_id: str, direction: str = Form(...)):
    db = get_supabase()
    artifact = _get_artifact_or_404(db, project_id)
    blocks = artifact["blocks"]
    index = next((i for i, b in enumerate(blocks) if b["id"] == block_id), None)
    if index is None:
        raise HTTPException(404, "block not found")

    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(blocks):
        blocks[index], blocks[target] = blocks[target], blocks[index]
        _save_blocks(db, artifact["id"], blocks)
        artifact["blocks"] = blocks
        artifact["current_version_id"] = None

    return _blocks_response(request, project_id, artifact)


@router.post("/blocks")
def add_block(request: Request, project_id: UUID, block_type: str = Form(...)):
    if block_type not in _DEFAULT_CONTENT:
        raise HTTPException(400, f"unknown block type: {block_type}")

    db = get_supabase()
    artifact = _get_artifact_or_404(db, project_id)
    new_block = {
        "id": f"blk_{uuid.uuid4().hex[:8]}",
        "type": block_type,
        "enabled": True,
        "content": _DEFAULT_CONTENT[block_type],
    }
    _block_adapter.validate_python(new_block)

    blocks = artifact["blocks"] + [new_block]
    _save_blocks(db, artifact["id"], blocks)
    artifact["blocks"] = blocks
    artifact["current_version_id"] = None
    return _blocks_response(request, project_id, artifact)


@router.delete("/blocks/{block_id}")
def delete_block(request: Request, project_id: UUID, block_id: str):
    db = get_supabase()
    artifact = _get_artifact_or_404(db, project_id)
    blocks = [b for b in artifact["blocks"] if b["id"] != block_id]
    if len(blocks) == len(artifact["blocks"]):
        raise HTTPException(404, "block not found")

    _save_blocks(db, artifact["id"], blocks)
    artifact["blocks"] = blocks
    artifact["current_version_id"] = None
    return _blocks_response(request, project_id, artifact)


@router.post("/save-version")
def save_version(project_id: UUID):
    db = get_supabase()
    artifact = _get_artifact_or_404(db, project_id)
    _create_version(db, artifact)
    return RedirectResponse(f"/projects/{project_id}/editor", status_code=303)


def _create_version(db, artifact: dict) -> dict:
    """Implements the exact 'Save Version' rule: always a new row, always repoints current_version_id."""
    existing = (
        db.table("artifact_versions")
        .select("version_number")
        .eq("artifact_id", artifact["id"])
        .order("version_number", desc=True)
        .limit(1)
        .execute()
        .data
    )
    next_number = (existing[0]["version_number"] + 1) if existing else 1
    version = (
        db.table("artifact_versions")
        .insert(
            {
                "artifact_id": artifact["id"],
                "version_number": next_number,
                "blocks": artifact["blocks"],
                "raw_html": (
                    strip_native_authoring_chrome(strip_hidden_slides(artifact["raw_html"]))
                    if artifact.get("raw_html")
                    else None
                ),
            }
        )
        .execute()
        .data[0]
    )
    db.table("artifacts").update({"current_version_id": version["id"]}).eq("id", artifact["id"]).execute()
    return version


@router.post("/mark-review")
def mark_review(project_id: UUID):
    db = get_supabase()
    project = db.table("projects").select("status").eq("id", str(project_id)).single().execute().data
    if project["status"] == "draft":
        db.table("projects").update({"status": "in_review"}).eq("id", str(project_id)).execute()
    return RedirectResponse(f"/projects/{project_id}", status_code=303)
