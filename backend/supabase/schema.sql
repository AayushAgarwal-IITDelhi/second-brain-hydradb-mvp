-- =============================================================================
-- Second Brain — Supabase schema (Phase 1)
-- =============================================================================
-- Run in the Supabase SQL editor or via `supabase db push`.
-- Idempotent: safe to re-run.
--
-- Creates:
--   public.profiles            (1:1 with auth.users)
--   public.workspaces          (tenants)
--   public.workspace_members   (user <-> workspace, with role)
--   public.workspace_role      (enum: owner | admin | member)
--
-- Plus:
--   set_updated_at trigger function
--   handle_new_user trigger on auth.users that auto-creates a profile +
--     personal workspace + owner membership row
--   row-level security policies using SECURITY DEFINER helpers (avoids
--     RLS recursion on workspace_members)
-- =============================================================================

-- Required extensions
create extension if not exists pgcrypto with schema extensions;

-- =============================================================================
-- profiles
-- =============================================================================
create table if not exists public.profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  email       text,
  full_name   text,
  avatar_url  text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists profiles_email_idx on public.profiles (email);

-- =============================================================================
-- workspace_role enum (created once)
-- =============================================================================
do $$
begin
  if not exists (
    select 1 from pg_type where typname = 'workspace_role'
  ) then
    create type public.workspace_role as enum ('owner', 'admin', 'member');
  end if;
end$$;

-- =============================================================================
-- workspaces
-- =============================================================================
create table if not exists public.workspaces (
  id                     uuid primary key default gen_random_uuid(),
  name                   text not null,
  slug                   text not null unique,
  owner_id               uuid references auth.users(id) on delete set null,
  -- Placeholders for Phase 2 (per-workspace HydraDB). Unused in Phase 1 —
  -- the backend still reads HYDRADB_TENANT_ID / HYDRADB_SUB_TENANT_ID from
  -- environment. Wiring these per-row is the very next migration.
  hydradb_tenant_id      text,
  hydradb_sub_tenant_id  text,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

create index if not exists workspaces_owner_idx on public.workspaces (owner_id);

-- =============================================================================
-- workspace_members
-- =============================================================================
create table if not exists public.workspace_members (
  workspace_id  uuid not null references public.workspaces(id) on delete cascade,
  user_id       uuid not null references auth.users(id) on delete cascade,
  role          public.workspace_role not null default 'member',
  created_at    timestamptz not null default now(),
  primary key (workspace_id, user_id)
);

create index if not exists workspace_members_user_idx
  on public.workspace_members (user_id);

-- =============================================================================
-- updated_at trigger function + triggers
-- =============================================================================
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end$$;

drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at
  before update on public.profiles
  for each row execute function public.set_updated_at();

drop trigger if exists workspaces_set_updated_at on public.workspaces;
create trigger workspaces_set_updated_at
  before update on public.workspaces
  for each row execute function public.set_updated_at();

-- =============================================================================
-- Membership helpers (SECURITY DEFINER → bypass RLS, avoid recursion)
-- =============================================================================
-- Used by RLS policies on workspaces and workspace_members. Without the
-- definer indirection, a policy that does
--   exists(select 1 from workspace_members where ...)
-- recurses through RLS on workspace_members. These helpers run as their
-- owner (postgres) and skip RLS, so the policy can ask a clean question.

create or replace function public.user_is_workspace_member(_workspace_id uuid)
returns boolean
language sql
security definer
set search_path = public
stable
as $$
  select exists(
    select 1
    from public.workspace_members
    where workspace_id = _workspace_id
      and user_id      = auth.uid()
  );
$$;

create or replace function public.user_owns_workspace(_workspace_id uuid)
returns boolean
language sql
security definer
set search_path = public
stable
as $$
  select exists(
    select 1
    from public.workspaces
    where id       = _workspace_id
      and owner_id = auth.uid()
  );
$$;

revoke all on function public.user_is_workspace_member(uuid) from public;
revoke all on function public.user_owns_workspace(uuid)      from public;

grant execute on function public.user_is_workspace_member(uuid)
  to anon, authenticated;
grant execute on function public.user_owns_workspace(uuid)
  to anon, authenticated;

-- =============================================================================
-- handle_new_user — auto-create profile + personal workspace + ownership
-- =============================================================================
-- Fires on every insert into auth.users (signup, magic link, OAuth). The
-- bot-token Slack ingestion in Phase 1 is still global; per-workspace
-- ingestion comes in Phase 2, at which point the hydradb_* columns above
-- get populated.

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  new_workspace_id  uuid;
  display_name      text;
  base_slug         text;
  candidate_slug    text;
  attempt           integer := 0;
begin
  display_name := coalesce(
    new.raw_user_meta_data->>'full_name',
    split_part(coalesce(new.email, ''), '@', 1),
    'user'
  );

  -- 1) profile (1:1 with auth.users)
  insert into public.profiles (id, email, full_name, avatar_url)
  values (
    new.id,
    new.email,
    display_name,
    new.raw_user_meta_data->>'avatar_url'
  )
  on conflict (id) do nothing;

  -- 2) Personal workspace. Slug derives from the email local-part, with
  --    a numeric suffix if that slug is already taken. Falls back to
  --    'workspace' if the address has no usable local-part.
  base_slug := regexp_replace(
    lower(split_part(coalesce(new.email, ''), '@', 1)),
    '[^a-z0-9-]+', '-', 'g'
  );
  base_slug := trim(both '-' from coalesce(base_slug, ''));
  if base_slug = '' then
    base_slug := 'workspace';
  end if;

  candidate_slug := base_slug;
  while exists (select 1 from public.workspaces where slug = candidate_slug) loop
    attempt := attempt + 1;
    candidate_slug := base_slug || '-' || attempt::text;
  end loop;

  insert into public.workspaces (name, slug, owner_id)
  values (
    display_name || '''s workspace',
    candidate_slug,
    new.id
  )
  returning id into new_workspace_id;

  -- 3) Owner membership row
  insert into public.workspace_members (workspace_id, user_id, role)
  values (new_workspace_id, new.id, 'owner');

  return new;
end$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- =============================================================================
-- Row-level security
-- =============================================================================
alter table public.profiles          enable row level security;
alter table public.workspaces        enable row level security;
alter table public.workspace_members enable row level security;

-- profiles ---------------------------------------------------------------
drop policy if exists profiles_self_select on public.profiles;
create policy profiles_self_select on public.profiles
  for select using (auth.uid() = id);

drop policy if exists profiles_self_update on public.profiles;
create policy profiles_self_update on public.profiles
  for update using (auth.uid() = id);

-- workspaces -------------------------------------------------------------
drop policy if exists workspaces_member_select on public.workspaces;
create policy workspaces_member_select on public.workspaces
  for select using (public.user_is_workspace_member(id));

drop policy if exists workspaces_authenticated_insert on public.workspaces;
create policy workspaces_authenticated_insert on public.workspaces
  for insert with check (auth.uid() = owner_id);

drop policy if exists workspaces_owner_update on public.workspaces;
create policy workspaces_owner_update on public.workspaces
  for update using (auth.uid() = owner_id);

drop policy if exists workspaces_owner_delete on public.workspaces;
create policy workspaces_owner_delete on public.workspaces
  for delete using (auth.uid() = owner_id);

-- workspace_members ------------------------------------------------------
-- Read: a member can see their own row and other members of workspaces
-- they belong to.
drop policy if exists workspace_members_select on public.workspace_members;
create policy workspace_members_select on public.workspace_members
  for select using (
    user_id = auth.uid()
    or public.user_is_workspace_member(workspace_id)
  );

-- Write: only the workspace owner can add/change/remove memberships.
-- (Service-role from the backend bypasses RLS and so isn't restricted.)
drop policy if exists workspace_members_owner_insert on public.workspace_members;
create policy workspace_members_owner_insert on public.workspace_members
  for insert with check (public.user_owns_workspace(workspace_id));

drop policy if exists workspace_members_owner_update on public.workspace_members;
create policy workspace_members_owner_update on public.workspace_members
  for update using (public.user_owns_workspace(workspace_id));

drop policy if exists workspace_members_owner_delete on public.workspace_members;
create policy workspace_members_owner_delete on public.workspace_members
  for delete using (public.user_owns_workspace(workspace_id));