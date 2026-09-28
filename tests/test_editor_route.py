"""End-to-end smoke test through the real ASGI app (TestClient), with a fake
Supabase client swapped into each router module. Catches template/route
wiring bugs that a plain Jinja2 render (or a hand-built Request) won't - e.g.
the starlette Jinja2Templates positional-argument order change that broke
every TemplateResponse call in this app during development (old style was
`TemplateResponse(name, context)`; the installed version requires
`TemplateResponse(request, name, context)`). Hits every page-rendering route
at least once since that bug was systemic, not isolated to one page.

Run: python tests/test_editor_route.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")
os.environ.setdefault("SESSION_SECRET", "test-secret")

from starlette.testclient import TestClient  # noqa: E402

from tests.fake_supabase import FakeSupabase  # noqa: E402

import app.auth as auth  # noqa: E402
import app.routers.clients as clients_router  # noqa: E402
import app.routers.editor as editor_router  # noqa: E402
import app.routers.projects as projects_router  # noqa: E402
import app.routers.publish as publish_router  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402

db = FakeSupabase()
for module in (clients_router, editor_router, projects_router, publish_router):
    module.get_supabase = lambda: db


def main():
    client = TestClient(fastapi_app)

    # Unauthenticated: /clients redirects to /login rather than rendering.
    resp = client.get("/clients", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login", resp.status_code
    resp = client.get("/login")
    assert resp.status_code == 200

    cookie_value = auth._serializer.dumps({"email": "test@example.com"})
    client.cookies.set(auth.COOKIE_NAME, cookie_value)

    client_id = str(uuid.uuid4())
    db.table("clients").insert({"id": client_id, "name": "Acme", "created_at": "2026-01-01T00:00:00Z"}).execute()
    assert client.get("/clients").status_code == 200
    assert client.get(f"/clients/{client_id}").status_code == 200

    project_id = str(uuid.uuid4())
    db.table("projects").insert(
        {"id": project_id, "client_id": client_id, "title": "Q3 Deck", "deliverable_type": "pptx",
         "status": "draft", "created_at": "2026-01-01T00:00:00Z"}
    ).execute()
    assert client.get(f"/projects/{project_id}").status_code == 200
    assert client.get(f"/projects/{project_id}/input").status_code == 200

    artifact_id = str(uuid.uuid4())
    db.table("artifacts").insert(
        {
            "id": artifact_id,
            "project_id": project_id,
            "current_version_id": None,
            "blocks": [
                {"id": "b1", "type": "kpi_grid", "enabled": True, "content": {
                    "title": "Metrics", "items": [{"label": "Rev", "value": 1, "unit": "USD", "change_pct": 1.2}]
                }},
                {"id": "b2", "type": "chart", "enabled": True, "content": {
                    "chart_type": "bar", "title": "Growth", "labels": ["Jan"], "series": [{"name": "S", "values": [1]}]
                }},
                {"id": "b3", "type": "comparison_table", "enabled": True, "content": {
                    "headers": ["Plan", "Price"], "rows": [["Basic", "$10"]]
                }},
                {"id": "b4", "type": "bullet_list", "enabled": True, "content": {"heading": "Notes", "items": ["one"]}},
            ],
        }
    ).execute()

    resp = client.get(f"/projects/{project_id}/editor")
    assert resp.status_code == 200, resp.text
    assert "Metrics" in resp.text and "Growth" in resp.text

    # Canvas view (Phase 3 addition): same blocks, alternate template, and the
    # view choice is remembered via cookie for the next plain /editor request.
    resp = client.get(f"/projects/{project_id}/editor?view=canvas")
    assert resp.status_code == 200 and 'id="canvas-list"' in resp.text and "Metrics" in resp.text, resp.text
    assert resp.cookies.get("editor_view") == "canvas"

    resp = client.get(f"/projects/{project_id}/editor")
    assert resp.status_code == 200 and 'id="canvas-list"' in resp.text, resp.text

    # Partial-swap endpoints follow the same cookie, so a canvas-view content
    # edit gets the canvas partial back, not the form-view one. Toggle b4
    # twice so its enabled state is unchanged for the assertions further down.
    resp = client.post(f"/projects/{project_id}/blocks/b4/toggle")
    assert resp.status_code == 200 and 'id="canvas-list"' in resp.text, resp.text
    client.post(f"/projects/{project_id}/blocks/b4/toggle")

    resp = client.get(f"/projects/{project_id}/editor?view=form")
    assert resp.status_code == 200 and 'id="blocks-list"' in resp.text and 'id="canvas-list"' not in resp.text, resp.text

    # Live preview pane renders the draft through the html renderer.
    resp = client.get(f"/projects/{project_id}/preview")
    assert resp.status_code == 200 and "Growth" in resp.text, resp.text

    # Structured kpi_grid edit (repeatable label/value/unit/change_pct rows, not pipe-delimited text).
    resp = client.post(
        f"/projects/{project_id}/blocks/b1/content",
        data={"title": "Metrics", "kpi_label": ["Rev", "Churn"], "kpi_value": ["2", "3"],
              "kpi_unit": ["USD", "%"], "kpi_change_pct": ["1.5", ""]},
    )
    assert resp.status_code == 200, resp.text
    artifact = db.table("artifacts").select("*").eq("id", artifact_id).single().execute().data
    kpi_block = next(b for b in artifact["blocks"] if b["id"] == "b1")
    assert len(kpi_block["content"]["items"]) == 2 and kpi_block["content"]["items"][1]["change_pct"] is None
    assert artifact["current_version_id"] is None

    # Structured chart edit via the JS-serialized chart_json grid.
    resp = client.post(
        f"/projects/{project_id}/blocks/b2/content",
        data={"chart_type": "line", "title": "Growth",
              "chart_json": '{"labels": ["Jan", "Feb"], "series": [{"name": "S", "values": [1, 2]}]}'},
    )
    assert resp.status_code == 200, resp.text
    artifact = db.table("artifacts").select("*").eq("id", artifact_id).single().execute().data
    chart_block = next(b for b in artifact["blocks"] if b["id"] == "b2")
    assert chart_block["content"]["labels"] == ["Jan", "Feb"]

    # Structured comparison_table edit via table_json.
    resp = client.post(
        f"/projects/{project_id}/blocks/b3/content",
        data={"table_json": '{"headers": ["Plan", "Price", "Seats"], "rows": [["Basic", "$10", "5"]]}'},
    )
    assert resp.status_code == 200, resp.text
    artifact = db.table("artifacts").select("*").eq("id", artifact_id).single().execute().data
    table_block = next(b for b in artifact["blocks"] if b["id"] == "b3")
    assert table_block["content"]["headers"] == ["Plan", "Price", "Seats"]

    # Toggle and move still work (Phase 3 core actions).
    resp = client.post(f"/projects/{project_id}/blocks/b4/toggle")
    assert resp.status_code == 200
    artifact = db.table("artifacts").select("*").eq("id", artifact_id).single().execute().data
    assert next(b for b in artifact["blocks"] if b["id"] == "b4")["enabled"] is False

    resp = client.post(f"/projects/{project_id}/blocks/b2/move", data={"direction": "up"})
    assert resp.status_code == 200
    artifact = db.table("artifacts").select("*").eq("id", artifact_id).single().execute().data
    assert artifact["blocks"][0]["id"] == "b2"

    resp = client.get(f"/projects/{project_id}")
    assert resp.status_code == 200, resp.text

    # Add a block that wasn't in the AI draft, then delete a different one.
    resp = client.post(f"/projects/{project_id}/blocks", data={"block_type": "text_block"})
    assert resp.status_code == 200, resp.text
    artifact = db.table("artifacts").select("*").eq("id", artifact_id).single().execute().data
    assert len(artifact["blocks"]) == 5
    new_block_id = next(b["id"] for b in artifact["blocks"] if b["type"] == "text_block" and b["id"] != "b4")

    resp = client.request("DELETE", f"/projects/{project_id}/blocks/{new_block_id}")
    assert resp.status_code == 200, resp.text
    artifact = db.table("artifacts").select("*").eq("id", artifact_id).single().execute().data
    assert len(artifact["blocks"]) == 4
    assert all(b["id"] != new_block_id for b in artifact["blocks"])

    # Publish (no password) then view the public share page and re-render the editor.
    resp = client.post(f"/projects/{project_id}/publish", data={"label": "Acme Corp", "password": ""}, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    share = db.table("shares").select("*").eq("artifact_id", artifact_id).limit(1).execute().data[0]
    resp = client.get(f"/s/{share['slug']}")
    assert resp.status_code == 200, resp.text
    assert "pptx" in resp.text.lower()

    resp = client.get(f"/projects/{project_id}/editor")
    assert resp.status_code == 200 and "Acme Corp" in resp.text and f"/s/{share['slug']}" in resp.text, resp.text

    # A second publish creates an independent second link, not a republish of the first.
    resp = client.post(f"/projects/{project_id}/publish", data={"label": "Beta Inc", "password": ""}, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    shares_now = db.table("shares").select("*").eq("artifact_id", artifact_id).execute().data
    assert len(shares_now) == 2, "publish must not overwrite the first client's link"

    # Republishing one link explicitly must not touch the other.
    resp = client.post(f"/projects/{project_id}/shares/{share['id']}/republish", data={"password": ""}, follow_redirects=False)
    assert resp.status_code == 303, resp.text

    # Deleting one link leaves the other live.
    other_share = next(s for s in shares_now if s["id"] != share["id"])
    resp = client.post(f"/projects/{project_id}/shares/{other_share['id']}/delete", follow_redirects=False)
    assert resp.status_code == 303, resp.text
    remaining = db.table("shares").select("*").eq("artifact_id", artifact_id).execute().data
    assert len(remaining) == 1 and remaining[0]["id"] == share["id"]

    # Password-protected share: gated until the correct password is posted.
    project2_id = str(uuid.uuid4())
    db.table("projects").insert(
        {"id": project2_id, "client_id": client_id, "title": "Secret Deck", "deliverable_type": "pptx",
         "status": "draft", "created_at": "2026-01-01T00:00:00Z"}
    ).execute()
    artifact2_id = str(uuid.uuid4())
    db.table("artifacts").insert(
        {"id": artifact2_id, "project_id": project2_id, "current_version_id": None,
         "blocks": [{"id": "b1", "type": "text_block", "enabled": True, "content": {"heading": "H", "body": "B"}}]}
    ).execute()
    client.post(f"/projects/{project2_id}/publish", data={"label": "Secret Client", "password": "hunter2"}, follow_redirects=False)
    share2 = db.table("shares").select("*").eq("artifact_id", artifact2_id).limit(1).execute().data[0]

    resp = client.get(f"/s/{share2['slug']}")
    assert resp.status_code == 200 and "password" in resp.text.lower()

    resp = client.post(f"/s/{share2['slug']}/password", data={"password": "wrong"})
    assert resp.status_code == 401

    resp = client.post(f"/s/{share2['slug']}/password", data={"password": "hunter2"}, follow_redirects=False)
    assert resp.status_code == 303
    resp = client.get(f"/s/{share2['slug']}")
    assert resp.status_code == 200 and "password" not in resp.text.lower()

    # App-wide sidebar (base.html): every authenticated page lists clients,
    # via a Jinja global wired to that router module's own (patchable)
    # get_supabase - a naive wiring to app.db's original would silently
    # bypass this test's fake and hit the network instead.
    resp = client.get("/clients")
    assert resp.status_code == 200 and f'href="/clients/{client_id}"' in resp.text, resp.text

    # Rename + change deliverable type, then delete - both redirect back to
    # the owning client and the delete actually removes the row (cascade to
    # inputs/artifacts/etc is enforced by the real DB's FK constraints, not
    # something this in-memory fake needs to simulate).
    resp = client.post(
        f"/projects/{project2_id}/update",
        data={"title": "Renamed Deck", "deliverable_type": "xlsx"},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"] == f"/clients/{client_id}", resp.headers
    updated = db.table("projects").select("*").eq("id", project2_id).single().execute().data
    assert updated["title"] == "Renamed Deck" and updated["deliverable_type"] == "xlsx"

    resp = client.post(f"/projects/{project2_id}/delete", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"/clients/{client_id}", resp.headers
    remaining = db.table("projects").select("*").eq("id", project2_id).execute().data
    assert remaining == []

    # Sidebar's nested client->project tree: the surviving project shows up
    # under its client, with a status dot and a rename/delete pair.
    resp = client.get("/clients")
    assert resp.status_code == 200 and f'href="/projects/{project_id}"' in resp.text, resp.text
    assert 'class="status-dot status-published"' in resp.text, resp.text

    # Client rename (via the sidebar's compact form, which must not blank
    # out contact fields it doesn't itself carry) and delete.
    resp = client.post(
        f"/clients/{client_id}/update",
        data={"name": "Acme Renamed", "contact_email": "", "contact_phone": "", "notes": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"] == f"/clients/{client_id}", resp.headers
    renamed = db.table("clients").select("*").eq("id", client_id).single().execute().data
    assert renamed["name"] == "Acme Renamed"

    resp = client.post(f"/clients/{client_id}/delete", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/clients", resp.headers
    remaining_clients = db.table("clients").select("*").eq("id", client_id).execute().data
    assert remaining_clients == []

    print("OK: every page-rendering route (auth, clients, projects, editor, publish, share) renders through the real ASGI app")


if __name__ == "__main__":
    main()
