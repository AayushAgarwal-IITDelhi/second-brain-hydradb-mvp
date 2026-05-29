-- =============================================================================
-- Phase 15: analytics_events (append-only telemetry)
-- =============================================================================
--
-- ONE table for ALL Phase 15 analytics signals. The `kind` column
-- discriminates between the event types:
--
--   kind = 'query_completed'    one row per /api/query call
--   kind = 'ingest_completed'   one row per Slack/Gmail ingest run
--   kind = 'memory_extracted'   one row per ingest extraction batch
--   kind = 'retrieval_failure'  one row per recall failure
--
-- Append-only. We never UPDATE rows -- old runs stay as historical
-- ground truth. The downstream aggregation helpers compute every
-- "stat" on the fly by COUNT/SUM over short time windows; with at
-- most ~thousands of events per workspace per day this is cheap.
--
-- Schema is intentionally compact -- one `payload jsonb` column
-- carries kind-specific extras (latency_ms, source_kind, etc.) so
-- a new event variant doesn't require a migration.
--
-- Workspace isolation: workspace_id NOT NULL + RLS enabled + NO
-- policies. Same pattern as extracted_memories / shared_links --
-- only the service-role backend can read or write this table.
-- =============================================================================

create table if not exists public.analytics_events (
    id              uuid primary key default gen_random_uuid(),
    workspace_id    uuid not null references public.workspaces(id) on delete cascade,

    kind            text not null
        check (kind in (
            'query_completed', 'ingest_completed',
            'memory_extracted', 'retrieval_failure'
        )),

    -- Common projections. We hoist a few fields out of `payload` so
    -- the aggregation queries don't have to dig into jsonb for the
    -- hot paths. Anything NOT in this list lives in `payload`.
    source_kind     text,        -- 'slack' | 'gmail' | null (for query events)
    latency_ms      integer,     -- for query_completed / ingest_completed
    success         boolean,     -- false = failure event

    payload         jsonb not null default '{}'::jsonb,

    created_at      timestamptz not null default now()
);

-- Lookup patterns:
--   - by (workspace_id, kind) + created_at range -> the dominant
--     aggregation query
--   - by (workspace_id, created_at) -> "recent activity" feed
create index if not exists analytics_events_workspace_kind_idx
    on public.analytics_events (workspace_id, kind, created_at desc);

create index if not exists analytics_events_workspace_created_idx
    on public.analytics_events (workspace_id, created_at desc);

-- RLS on, no policies -> service-role backend only.
alter table public.analytics_events enable row level security;