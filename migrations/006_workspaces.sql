-- Multi-tenant workspaces: public registration (see app/auth.py's sign_up())
-- creates a new isolated workspace per signup, or joins an existing one via
-- a shareable invite code. Every client/project now belongs to exactly one
-- workspace; every admin-side query filters by it (see app/scoping.py) so
-- one workspace's clients/projects are never visible to another's members.
--
-- Supabase Auth owns auth.users - this maps each auth user to the single
-- workspace they belong to (one workspace per user, no cross-workspace
-- membership), so every scoping check is a single workspace_id equality,
-- and email is duplicated here so the members list doesn't need an Admin
-- API round-trip to auth.users just to show who's on the team.
create table workspaces (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  invite_code text not null unique,
  created_at timestamptz not null default now()
);

create table workspace_members (
  user_id uuid primary key,
  workspace_id uuid not null references workspaces(id) on delete cascade,
  email text not null,
  role text not null default 'owner',
  created_at timestamptz not null default now()
);

-- Nullable for now: existing clients/projects predate workspaces and need a
-- one-time backfill (a default workspace for the existing admin user) before
-- these can safely become NOT NULL. See the backfill note in TECHNICAL.md /
-- the migration instructions given alongside this file.
alter table clients add column workspace_id uuid references workspaces(id);
alter table projects add column workspace_id uuid references workspaces(id);
create index clients_workspace_id_idx on clients(workspace_id);
create index projects_workspace_id_idx on projects(workspace_id);
