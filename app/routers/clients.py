from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import current_user
from app.db import get_supabase, sidebar_clients
from app.models.schemas import DeliverableType

router = APIRouter(prefix="/clients", tags=["clients"], dependencies=[Depends(current_user)])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["sidebar_clients"] = lambda: sidebar_clients(get_supabase())


@router.get("")
def list_clients(request: Request):
    clients = get_supabase().table("clients").select("*").order("created_at", desc=True).execute().data
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
        }
    ).execute()
    return RedirectResponse("/clients", status_code=303)


@router.get("/{client_id}")
def client_detail(request: Request, client_id: UUID):
    db = get_supabase()
    client = db.table("clients").select("*").eq("id", str(client_id)).single().execute().data
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
def create_project(client_id: UUID, title: str = Form(...), deliverable_type: DeliverableType = Form(...)):
    get_supabase().table("projects").insert(
        {"client_id": str(client_id), "title": title, "deliverable_type": deliverable_type}
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
    get_supabase().table("clients").update(
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
    client = db.table("clients").select("id").eq("id", str(client_id)).execute().data
    if not client:
        raise HTTPException(404, "client not found")
    db.table("clients").delete().eq("id", str(client_id)).execute()
    return RedirectResponse("/clients", status_code=303)
