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
    clients to make N+1 worth avoiding.

    Scoped to the current workspace (see app.auth.current_workspace_id) -
    the projects sub-query below doesn't need its own workspace filter,
    since a client's projects always carry the same workspace_id as the
    client itself."""
    from app.auth import current_workspace_id

    clients = (
        db.table("clients")
        .select("id, name, contact_email, contact_phone, notes")
        .eq("workspace_id", current_workspace_id())
        .order("name")
        .execute()
        .data
    )
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


def current_workspace_info(db) -> dict:
    """Backs the /settings page: the current workspace's name, invite code
    (see migrations/006_workspaces.sql), and member list - so the owner can
    copy the invite link and see who's already on the team."""
    from app.auth import current_workspace_id

    workspace_id = current_workspace_id()
    workspace = db.table("workspaces").select("*").eq("id", workspace_id).single().execute().data
    members = (
        db.table("workspace_members")
        .select("email, role, created_at")
        .eq("workspace_id", workspace_id)
        .order("created_at")
        .execute()
        .data
    )
    return {"name": workspace["name"], "invite_code": workspace["invite_code"], "members": members}
