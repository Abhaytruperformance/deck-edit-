-- Multi-share support: a project can already have more than one row in
-- `shares` for the same artifact (no unique constraint on artifact_id was
-- ever added), but the application layer only ever created/reused a single
-- one. This adds the one column actually missing for a real multi-link
-- workflow - a human-readable label so a project with several live links
-- (one per client) can tell them apart in the UI. Nullable: existing shares
-- predate this and simply show as unlabeled until someone renames them.
alter table shares add column label text;
