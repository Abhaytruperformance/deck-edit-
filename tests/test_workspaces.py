"""Multi-tenant workspace logic (see migrations/006_workspaces.sql):
sign_up()/sign_in() in app/auth.py, and the scoped lookups in app/scoping.py
that keep one workspace's clients/projects invisible to another's members.

Run: python tests/test_workspaces.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")
os.environ.setdefault("SESSION_SECRET", "test-secret")

from fastapi import HTTPException  # noqa: E402

from tests.fake_supabase import FakeSupabase  # noqa: E402

import app.auth as auth  # noqa: E402
import app.db as db_module  # noqa: E402
import app.scoping as scoping  # noqa: E402


def main():
    db = FakeSupabase()
    auth.get_supabase = lambda: db

    # 1. A fresh sign_up() with no invite code creates its own new workspace
    #    and becomes its owner.
    cookie_a = auth.sign_up("owner@acme.test", "pw", "Acme Agency", "")
    payload_a = auth._serializer.loads(cookie_a)
    workspace_a = payload_a["workspace_id"]
    members_a = db.table("workspace_members").select("*").eq("workspace_id", workspace_a).execute().data
    assert len(members_a) == 1 and members_a[0]["role"] == "owner"
    workspace_row = db.table("workspaces").select("*").eq("id", workspace_a).single().execute().data
    assert workspace_row["name"] == "Acme Agency"
    invite_code = workspace_row["invite_code"]

    # 2. A second, independent sign_up() with no invite code gets its OWN
    #    separate workspace - registrations don't collide by default.
    cookie_b = auth.sign_up("owner@other.test", "pw", "Other Co", "")
    workspace_b = auth._serializer.loads(cookie_b)["workspace_id"]
    assert workspace_b != workspace_a

    # 3. Signing up WITH workspace_a's invite code joins that same workspace,
    #    as a member (not a second owner).
    cookie_c = auth.sign_up("teammate@acme.test", "pw", "", invite_code)
    workspace_c = auth._serializer.loads(cookie_c)["workspace_id"]
    assert workspace_c == workspace_a, "an invite code must join the SAME workspace, not create a new one"
    members_a = db.table("workspace_members").select("*").eq("workspace_id", workspace_a).execute().data
    assert len(members_a) == 2
    teammate = next(m for m in members_a if m["email"] == "teammate@acme.test")
    assert teammate["role"] == "member"

    # 4. A bogus invite code fails closed rather than silently creating a
    #    new workspace (which would strand the user with no realized intent).
    try:
        auth.sign_up("nobody@acme.test", "pw", "", "not-a-real-code")
        raise AssertionError("a bogus invite code must not succeed")
    except ValueError:
        pass

    # 5. sign_in() for an existing member resolves the same workspace_id
    #    sign_up() gave them.
    cookie = auth.sign_in("teammate@acme.test", "pw")
    assert auth._serializer.loads(cookie)["workspace_id"] == workspace_a

    # 6. Workspace isolation: a client seeded under workspace_a is invisible
    #    (404) from workspace_b's context, and visible from workspace_a's.
    client_id = db.table("clients").insert({"name": "Client X", "workspace_id": workspace_a}).execute().data[0]["id"]

    auth._session_ctx.set({"email": "owner@other.test", "workspace_id": workspace_b})
    try:
        scoping.get_client_or_404(db, client_id)
        raise AssertionError("workspace_b must not see workspace_a's client")
    except HTTPException as e:
        assert e.status_code == 404

    auth._session_ctx.set({"email": "owner@acme.test", "workspace_id": workspace_a})
    found = scoping.get_client_or_404(db, client_id)
    assert found["name"] == "Client X"

    # 7. sidebar_clients() only lists the current workspace's clients, even
    #    though both workspaces have data seeded.
    db.table("clients").insert({"name": "Client Y", "workspace_id": workspace_b}).execute()
    auth._session_ctx.set({"email": "owner@acme.test", "workspace_id": workspace_a})
    names = {c["name"] for c in db_module.sidebar_clients(db)}
    assert names == {"Client X"}, f"sidebar_clients() leaked across workspaces: {names}"

    print("OK: workspace sign-up/sign-in/invite-join and cross-workspace isolation all hold")


if __name__ == "__main__":
    main()
