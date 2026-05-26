-- =============================================================================
-- Second Brain — Supabase schema (Phase 7: production hardening)
-- =============================================================================
-- Apply AFTER schema.sql + phase2 + phase3 + phase4. Idempotent.
--
-- Creates:
--   public.slack_event_seen  — durable dedupe for Slack webhook deliveries
--
-- Why a table for this:
--   Phase 5 stored seen event_ids in an in-process dict. That works
--   for a single uvicorn process but lets duplicates through across
--   restarts and across workers. Slack retries a failed delivery up
--   to 3 times within an hour, so a restart at the wrong moment
--   ingested the same message twice.
--
--   We persist (event_id, workspace_id, seen_at) so any worker — or
--   a restarted worker — can claim an event_id atomically. The
--   workspace_id lets us partition cleanup by tenant if we ever need
--   to, and is useful when inspecting an incident.
--
-- TTL:
--   Slack's retry window is 1 hour. We keep rows for 24 hours so an
--   operator looking at logs the next morning can correlate. After
--   that the cleanup helper drops them.
-- =============================================================================


create table if not exists public.slack_event_seen (
  -- Slack's event_id is globally unique (Ev0123ABCD format). Used as
  -- the PK so insert-on-conflict is the dedupe primitive.
  event_id      text primary key,

  -- Nullable: we record the workspace_id when we can resolve it from
  -- team_id, but the dedupe insert happens BEFORE workspace lookup so
  -- duplicates from unknown teams are also short-circuited.
  workspace_id  uuid references public.workspaces(id) on delete cascade,

  seen_at       timestamptz not null default now()
);

create index if not exists slack_event_seen_seen_at_idx
  on public.slack_event_seen (seen_at);

create index if not exists slack_event_seen_workspace_idx
  on public.slack_event_seen (workspace_id, seen_at desc)
  where workspace_id is not null;


-- =============================================================================
-- Cleanup helper. Call from a cron / scheduler job (or invoke ad-hoc).
-- =============================================================================
-- Deletes rows older than the given retention window. Defaults to 24h
-- which is plenty: Slack's retry window is 1h, the extra 23h is for
-- incident-investigation breathing room.

create or replace function public.cleanup_slack_event_seen(retain_hours int default 24)
returns integer
language plpgsql
as $$
declare
  removed integer;
begin
  delete from public.slack_event_seen
    where seen_at < now() - (retain_hours || ' hours')::interval;
  get diagnostics removed = row_count;
  return removed;
end$$;


-- =============================================================================
-- RLS: deny all client access. The service-role backend bypasses RLS.
-- =============================================================================
-- This table holds no user-visible data; only the backend writes to
-- it. We enable RLS WITHOUT any policies so any authenticated client
-- query fails closed.

alter table public.slack_event_seen enable row level security;