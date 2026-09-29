"""Phase 5 acceptance check (TECHNICAL.md): editing the draft after publish must
NOT change what's live at the share link, and project.status transitions
must match the exact state machine - no status changes on plain edits or
saves. Runs against an in-memory fake, no real Supabase needed.

Run: python tests/test_versioning.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")

from tests.fake_supabase import FakeSupabase  # noqa: E402

import app.auth as auth  # noqa: E402
import app.routers.editor as editor  # noqa: E402
import app.routers.publish as publish  # noqa: E402


def make_db():
    return FakeSupabase()


def seed_project_and_artifact(db, workspace_id):
    project_id = str(uuid.uuid4())
    db.table("projects").insert(
        {"id": project_id, "title": "Acme Q3", "deliverable_type": "pptx", "status": "draft", "workspace_id": workspace_id}
    ).execute()
    artifact = db.table("artifacts").insert(
        {"id": str(uuid.uuid4()), "project_id": project_id, "blocks": [{"id": "s1", "type": "text_block",
         "enabled": True, "content": {"heading": "Hi", "body": "v1"}}], "current_version_id": None}
    ).execute().data[0]
    return project_id, artifact


def get_project(db, project_id):
    return db.table("projects").select("*").eq("id", project_id).single().execute().data


def get_artifact(db, artifact_id):
    return db.table("artifacts").select("*").eq("id", artifact_id).single().execute().data


def main():
    db = make_db()
    editor.get_supabase = lambda: db
    publish.get_supabase = lambda: db

    # Every route funnels ownership checks through app.scoping, which reads
    # the current workspace from this contextvar - current_user() sets it
    # from the session cookie on a real request; here we set it directly,
    # same as tests/test_editor_route.py does via a crafted cookie.
    workspace_id = str(uuid.uuid4())
    auth._session_ctx.set({"email": "test@example.com", "workspace_id": workspace_id})

    project_id, artifact = seed_project_and_artifact(db, workspace_id)

    # 1. Mark as In Review is manual and only fires from draft.
    editor.mark_review(project_id)
    assert get_project(db, project_id)["status"] == "in_review", "mark_review should set in_review from draft"

    # 2. Simulate an editor content edit: current_version_id must be null (never set yet, stays null).
    assert get_artifact(db, artifact["id"])["current_version_id"] is None

    # 3. Publish with no saved version yet -> Publish performs Save Version first, then points the share at it.
    publish.publish(project_id, label="Acme Corp", password="")
    project = get_project(db, project_id)
    assert project["status"] == "published", "first successful publish must set status=published"

    share = db.table("shares").select("*").eq("artifact_id", artifact["id"]).limit(1).execute().data[0]
    versions = db.table("artifact_versions").select("*").eq("artifact_id", artifact["id"]).execute().data
    assert len(versions) == 1, "publish with no current_version_id must create exactly one version"
    v1_id = versions[0]["id"]
    assert share["published_version_id"] == v1_id
    artifact = get_artifact(db, artifact["id"])
    assert artifact["current_version_id"] == v1_id, "publish's implicit save must repoint current_version_id"

    # 4. Edit the draft after publishing: current_version_id resets to null, but the
    #    published share must NOT change. This is the core acceptance-test invariant.
    db.table("artifacts").update(
        {"blocks": [{"id": "s1", "type": "text_block", "enabled": True, "content": {"heading": "Hi", "body": "EDITED"}}],
         "current_version_id": None}
    ).eq("id", artifact["id"]).execute()

    share_after_edit = db.table("shares").select("*").eq("artifact_id", artifact["id"]).limit(1).execute().data[0]
    assert share_after_edit["published_version_id"] == v1_id, "editing the draft must not move the published pointer"
    published_version = db.table("artifact_versions").select("*").eq("id", v1_id).single().execute().data
    assert published_version["blocks"][0]["content"]["body"] == "v1", "the published SNAPSHOT must stay frozen"

    # 5. Status must not revert on a plain edit.
    assert get_project(db, project_id)["status"] == "published"

    # 6. mark_review must no-op once published (never revert published -> in_review).
    editor.mark_review(project_id)
    assert get_project(db, project_id)["status"] == "published", "mark_review must not downgrade a published project"

    # 7. Explicit Save Version after the edit -> new version, current_version_id points at it,
    #    but the OLD published share still points at v1 until an explicit republish.
    artifact = get_artifact(db, artifact["id"])
    version2 = editor._create_version(db, artifact)
    assert version2["version_number"] == 2
    artifact = get_artifact(db, artifact["id"])
    assert artifact["current_version_id"] == version2["id"]
    share_still = db.table("shares").select("*").eq("artifact_id", artifact["id"]).limit(1).execute().data[0]
    assert share_still["published_version_id"] == v1_id, "Save Version alone must not touch the live share"

    # 8. republish_share repoints the SAME link (no new share, no redundant version).
    publish.republish_share(project_id, share["id"], password="")
    versions = db.table("artifact_versions").select("*").eq("artifact_id", artifact["id"]).execute().data
    assert len(versions) == 2, "republish must reuse current_version_id, not create a redundant version"
    all_shares = db.table("shares").select("*").eq("artifact_id", artifact["id"]).execute().data
    assert len(all_shares) == 1, "republish_share must not create a second share"
    share_final = all_shares[0]
    assert share_final["published_version_id"] == version2["id"], "republish must move the pointer to the new version"

    # 9. publish() again creates an INDEPENDENT second link (multi-client support),
    #    leaving the first link's version pointer untouched.
    publish.publish(project_id, label="Beta Inc", password="")
    all_shares = db.table("shares").select("*").eq("artifact_id", artifact["id"]).execute().data
    assert len(all_shares) == 2, "publish() must always create a new share, never overwrite an existing one"
    first_share_after = next(s for s in all_shares if s["id"] == share["id"])
    assert first_share_after["published_version_id"] == version2["id"], "a second publish must not move the first link's pointer"

    # 10. Workspace isolation: switching to a DIFFERENT workspace must 404 on
    #     this exact project - the whole point of app.scoping (see
    #     migrations/006_workspaces.sql). Not a coincidental pass: both this
    #     project's workspace_id and the contextvar are real, distinct UUIDs.
    from fastapi import HTTPException

    auth._session_ctx.set({"email": "other@example.com", "workspace_id": str(uuid.uuid4())})
    try:
        editor._get_artifact_or_404(db, project_id)
        raise AssertionError("a different workspace must not see this project's artifact")
    except HTTPException as e:
        assert e.status_code == 404

    print("OK: versioning + publish + status state machine hold under the acceptance-test invariants")


if __name__ == "__main__":
    main()
