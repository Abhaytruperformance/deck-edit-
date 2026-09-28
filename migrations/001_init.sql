-- Client Deliverables Platform: initial schema
-- Single internal workspace, no multi-tenancy.

create extension if not exists pgcrypto;

create table clients (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    contact_email text,
    contact_phone text,
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create type deliverable_type as enum ('pptx', 'xlsx', 'animated_html');
create type project_status as enum ('draft', 'in_review', 'published');

create table projects (
    id uuid primary key default gen_random_uuid(),
    client_id uuid not null references clients(id) on delete cascade,
    title text not null,
    deliverable_type deliverable_type not null,
    status project_status not null default 'draft',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create type input_source_type as enum ('form', 'file_upload');

create table inputs (
    id uuid primary key default gen_random_uuid(),
    project_id uuid not null references projects(id) on delete cascade,
    source_type input_source_type not null,
    raw_data jsonb not null,
    version int not null default 1,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- artifact_versions before artifacts because artifacts.current_version_id
-- references it (nullable FK, resolved via alter table below).
create table artifact_versions (
    id uuid primary key default gen_random_uuid(),
    artifact_id uuid not null, -- FK to artifacts added after artifacts exists
    version_number int not null,
    blocks jsonb not null,
    created_at timestamptz not null default now()
);

create table artifacts (
    id uuid primary key default gen_random_uuid(),
    project_id uuid not null references projects(id) on delete cascade,
    blocks jsonb not null default '[]'::jsonb,
    current_version_id uuid references artifact_versions(id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table artifact_versions
    add constraint artifact_versions_artifact_id_fkey
    foreign key (artifact_id) references artifacts(id) on delete cascade;

create table shares (
    id uuid primary key default gen_random_uuid(),
    artifact_id uuid not null references artifacts(id) on delete cascade,
    published_version_id uuid references artifact_versions(id),
    slug text not null unique,
    password_hash text,
    expires_at timestamptz,
    view_count int not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index on projects (client_id);
create index on inputs (project_id);
create index on artifacts (project_id);
create index on artifact_versions (artifact_id);
create index on shares (artifact_id);
