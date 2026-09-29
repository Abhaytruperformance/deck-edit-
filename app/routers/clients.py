from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.assets import style_css_version
from app.auth import current_user, current_user_email, current_workspace_id
from app.db import get_supabase, sidebar_clients
from app.models.schemas import DeliverableType
from app.scoping import get_client_or_404

router = APIRouter(prefix="/clients", tags=["clients"], dependencies=[Depends(current_user)])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["sidebar_clients"] = lambda: sidebar_clients(get_supabase())
templates.env.globals["style_v"] = style_css_version
templates.env.globals["current_user_email"] = current_user_email


@router.get("")
def list_clients(request: Request):
    clients = (
        get_supabase().table("clients").select("*").eq("workspace_id", current_workspace_id())
        .order("created_at", desc=True).execute().data
    )
    return templates.TemplateResponse(request, "clients/list.html", {"clients": clients})


@router.post("")
def create_client(
    name: str = Form(...),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    notes: str = Form(""),
):
    get_supabase().table("clients").insert(
        {
            "name": name,
            "contact_email": contact_email or None,
            "contact_phone": contact_phone or None,
            "notes": notes or None,
            "workspace_id": current_workspace_id(),
        }
    ).execute()
    return RedirectResponse("/clients", status_code=303)


@router.get("/{client_id}")
def client_detail(request: Request, client_id: UUID):
    db = get_supabase()
    client = get_client_or_404(db, client_id)
    projects = (
        db.table("projects")
        .select("*")
        .eq("client_id", str(client_id))
        .order("created_at", desc=True)
        .execute()
        .data
    )
    return templates.TemplateResponse(
        request, "clients/detail.html", {"client": client, "projects": projects}
    )


@router.post("/{client_id}/projects")
def create_project(client_id: UUID, title: str = Form(...), deliverable_type: DeliverableType = Form("animated_html")):
    db = get_supabase()
    get_client_or_404(db, client_id)
    db.table("projects").insert(
        {
            "client_id": str(client_id),
            "title": title,
            "deliverable_type": deliverable_type,
            "workspace_id": current_workspace_id(),
        }
    ).execute()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/{client_id}/update")
def update_client(
    client_id: UUID,
    name: str = Form(...),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    notes: str = Form(""),
):
    db = get_supabase()
    get_client_or_404(db, client_id)
    db.table("clients").update(
        {
            "name": name,
            "contact_email": contact_email or None,
            "contact_phone": contact_phone or None,
            "notes": notes or None,
        }
    ).eq("id", str(client_id)).execute()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/{client_id}/delete")
def delete_client(client_id: UUID):
    """Cascades through projects (and, transitively, everything under them)
    via the FK constraints in migrations/001_init.sql - no manual cleanup
    needed."""
    db = get_supabase()
    get_client_or_404(db, client_id)
    db.table("clients").delete().eq("id", str(client_id)).execute()
    return RedirectResponse("/clients", status_code=303)
