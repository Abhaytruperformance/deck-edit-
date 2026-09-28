-- Adds raw_html storage for the "exact visual clone, edited in place" mode:
-- an uploaded/imported HTML deck kept as its own literal markup instead of
-- being reinterpreted into the block schema. NULL raw_html (the default)
-- means the artifact is block-based as before; when it's set, the editor
-- and publish/share routes serve/edit it directly instead of rendering
-- from blocks.
alter table artifacts add column raw_html text;
alter table artifact_versions add column raw_html text;
