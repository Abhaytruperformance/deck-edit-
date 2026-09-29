"""Workspace-scoped lookups shared across routers. Every route that takes a
client_id/project_id from the URL must resolve it through one of these, not
a bare `db.table(...).eq("id", ...)` - skipping it isn't just a bug, it's a
cross-tenant data leak (any logged-in user could read/edit another
workspace's client or project by guessing its UUID).

Reads the current workspace from app.auth's contextvar rather than taking it
as a parameter, so callers don't need `user: dict = Depends(current_user)`
threaded through every single route signature - the router-level
`dependencies=[Depends(current_user)]` already guarantees it's set before
any route body runs.
"""
from uuid import UUID

from fastapi import HTTPException

from app.auth import current_workspace_id


def get_client_or_404(db, client_id: UUID | str) -> dict:
    rows = (
        db.table("clients")
        .select("*")
        .eq("id", str(client_id))
        .eq("workspace_id", current_workspace_id())
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(404, "client not found")
    return rows[0]


def get_project_or_404(db, project_id: UUID | str) -> dict:
    rows = (
        db.table("projects")
        .select("*")
        .eq("id", str(project_id))
        .eq("workspace_id", current_workspace_id())
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(404, "project not found")
    return rows[0]
