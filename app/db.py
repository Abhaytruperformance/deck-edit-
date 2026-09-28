from functools import lru_cache

from supabase import Client, create_client

from app.config import settings


@lru_cache
def get_supabase() -> Client:
    return create_client(settings.supabase_url, settings.supabase_key)


def sidebar_clients(db) -> list[dict]:
    """Query behind base.html's app-wide sidebar: clients with their
    projects nested under them, for the collapsible client->project tree.
    Takes `db` rather than calling get_supabase() itself so each router can
    register it as a Jinja global bound to *its own* (test-patchable)
    get_supabase name - see the `templates.env.globals["sidebar_clients"]`
    wiring in clients.py/projects.py/editor.py. One query per client is
    fine here - an internal tool's sidebar, not a hot path with enough
    clients to make N+1 worth avoiding."""
    clients = db.table("clients").select("id, name, contact_email, contact_phone, notes").order("name").execute().data
    for c in clients:
        c["projects"] = (
            db.table("projects")
            .select("id, title, status, deliverable_type")
            .eq("client_id", c["id"])
            .order("created_at", desc=True)
            .execute()
            .data
        )
    return clients
