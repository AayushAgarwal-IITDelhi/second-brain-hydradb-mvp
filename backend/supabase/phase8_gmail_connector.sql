-- =============================================================================
-- Second Brain — Supabase schema (Phase 8: Gmail connector)
-- =============================================================================
-- Apply AFTER schema.sql + phase2 + phase3 + phase4 + phase7. Idempotent.
--
-- Creates per-workspace Gmail connection state. Mirrors the Slack
-- design (Phase 3 slack_installations + slack_channels) so the mental
-- model stays consistent:
--
--   gmail_connections    -- one row per (workspace_id, google_user_id)
--   gmail_labels         -- per-connection label list + selection toggles
--   gmail_ingestion_state-- per (connection, label) sync watermark
--
-- Why one row per (workspace, google_account) instead of one per
-- workspace:
--   Real teams often have several mailboxes -- a personal account
--   plus a shared "support@" account, for instance. Allowing multiple
--   connections is cheap (the UI lists them) and avoids forcing the
--   user to choose just one.
--
-- Token storage:
--   refresh_token + access_token are stored in plain text for Phase 8.
--   This matches the Slack approach (slack_installations.bot_token).
--   Encrypt-at-rest with pgsodium is a follow-up. We mitigate via:
--     - RLS denies all client access (only service-role can read).
--     - The tokens never leave the backend in any API response (see
--       supabase_client.get_gmail_connection_public).
-- =============================================================================


-- =============================================================================
-- gmail_connections
-- =============================================================================
create table if not exists public.gmail_connections (
  id                uuid primary key default gen_random_uuid(),
  workspace_id      uuid not null references public.workspaces(id) on delete cascade,

  -- Google's user identifier ("sub" claim from id_token / userinfo).
  -- Stable across renames + display-name changes.
  google_user_id    text not null,
  email             text not null default '',

  -- Token material. access_token is short-lived (~1h); refresh_token
  -- lasts indefinitely until revoked. Both are server-side secrets.
  access_token      text not null default '',
  refresh_token     text not null default '',
  token_expiry      timestamptz,

  -- Space-separated scope string returned by Google.
  scopes            text not null default '',

  -- 'active' | 'revoked' | 'error'. The frontend uses this to render
  -- "Reconnect Gmail" when the refresh token has been revoked.
  status            text not null default 'active',

  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),

  -- One row per (workspace, Google account). Reconnecting the SAME
  -- Google account in the SAME workspace updates this row in place.
  unique (workspace_id, google_user_id)
);

create index if not exists gmail_connections_workspace_idx
  on public.gmail_connections (workspace_id);

drop trigger if exists gmail_connections_set_updated_at
  on public.gmail_connections;
create trigger gmail_connections_set_updated_at
  before update on public.gmail_connections
  for each row execute function public.set_updated_at();


-- =============================================================================
-- gmail_labels
-- =============================================================================
-- One row per (connection, label_id). `type` is one of Gmail's
-- system/user values: 'system' (INBOX, SENT, SPAM, TRASH, ...) or
-- 'user' (anything the user created).
create table if not exists public.gmail_labels (
  id                    uuid primary key default gen_random_uuid(),
  workspace_id          uuid not null references public.workspaces(id) on delete cascade,
  gmail_connection_id   uuid not null references public.gmail_connections(id) on delete cascade,

  label_id              text not null,
  name                  text not null default '',
  type                  text not null default 'user',

  is_selected           boolean not null default false,
  updated_at            timestamptz not null default now(),

  unique (gmail_connection_id, label_id)
);

create index if not exists gmail_labels_workspace_idx
  on public.gmail_labels (workspace_id);
create index if not exists gmail_labels_connection_idx
  on public.gmail_labels (gmail_connection_id);

drop trigger if exists gmail_labels_set_updated_at
  on public.gmail_labels;
create trigger gmail_labels_set_updated_at
  before update on public.gmail_labels
  for each row execute function public.set_updated_at();


-- =============================================================================
-- gmail_ingestion_state
-- =============================================================================
-- Per-(connection, label) sync watermark. Gmail's history API returns
-- a `historyId` checkpoint per response; storing the highest one we've
-- ingested lets the next pass call users.history.list?startHistoryId=...
-- and pull only what changed.
--
-- For the MVP we also fall back to `last_synced_at` and a simple
-- recency cap so a deploy that never ran the history endpoint still
-- avoids re-ingesting the whole label.
create table if not exists public.gmail_ingestion_state (
  id                    uuid primary key default gen_random_uuid(),
  workspace_id          uuid not null references public.workspaces(id) on delete cascade,
  gmail_connection_id   uuid not null references public.gmail_connections(id) on delete cascade,
  label_id              text not null,

  last_history_id       text,
  last_synced_at        timestamptz,

  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),

  unique (gmail_connection_id, label_id)
);

create index if not exists gmail_ingestion_state_workspace_idx
  on public.gmail_ingestion_state (workspace_id);

drop trigger if exists gmail_ingestion_state_set_updated_at
  on public.gmail_ingestion_state;
create trigger gmail_ingestion_state_set_updated_at
  before update on public.gmail_ingestion_state
  for each row execute function public.set_updated_at();


-- =============================================================================
-- Row-level security
-- =============================================================================
alter table public.gmail_connections      enable row level security;
alter table public.gmail_labels           enable row level security;
alter table public.gmail_ingestion_state  enable row level security;

-- gmail_connections -------------------------------------------------------
-- Read/write: NONE. Holds refresh tokens; backend-only access via the
-- service-role key. No policies means RLS fails closed for any
-- authenticated client.

-- gmail_labels ------------------------------------------------------------
-- Read: workspace members can list their workspace's labels (used by
-- the picker UI when we eventually build it). Writes still go through
-- the backend.
drop policy if exists gmail_labels_member_select on public.gmail_labels;
create policy gmail_labels_member_select on public.gmail_labels
  for select using (public.user_is_workspace_member(workspace_id));

-- gmail_ingestion_state ---------------------------------------------------
-- Read/write: NONE -- internal bookkeeping, backend-only.