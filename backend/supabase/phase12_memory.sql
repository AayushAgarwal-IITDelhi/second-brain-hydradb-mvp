-- =============================================================================
-- Phase 12: extracted memories (structured "Second Brain" layer)
-- =============================================================================
--
-- Single-table store for everything the deterministic memory extractor
-- pulls out of ingested Slack messages / threads / Gmail emails:
--
--   kind = 'action_item'   "Rahul will deploy Friday"
--   kind = 'decision'      "we agreed to use Railway"
--   kind = 'summary'       short thread-level / email-level recap
--   kind = 'entity'        "Kafka", "rollout-doc", "Rahul"
--
-- One table is intentional. A separate-table-per-kind design would 4x
-- the migration surface, complicate RLS, and offer no semantic gain --
-- the consumer always filters by `kind` anyway. The `metadata` jsonb
-- column absorbs kind-specific fields (e.g. action_items use `owner`,
-- entities use `entity_type`) without schema churn.
--
-- Dedupe / idempotency
-- --------------------
-- UNIQUE(workspace_id, kind, content_hash, source_stable_key) means:
--   - The same action item said twice in the SAME message -> one row
--   - The same action item said in two different messages -> two rows
--     (intentional: traceability requires both source links)
--   - Re-ingesting the same source -> upsert no-ops the existing row
--
-- content_hash is the SHA-256 hex of the canonical lowercased + whitespace-
-- normalized content text. The extractor computes it; the table just
-- enforces uniqueness.
--
-- Workspace isolation
-- -------------------
-- workspace_id NOT NULL + RLS enabled + NO policies. Same pattern as
-- gmail_connections + slack_event_seen -- only the service-role backend
-- ever reads or writes this table.
-- =============================================================================

create table if not exists public.extracted_memories (
    id                   uuid primary key default gen_random_uuid(),
    workspace_id         uuid not null references public.workspaces(id) on delete cascade,

    kind                 text not null
        check (kind in ('action_item', 'decision', 'summary', 'entity')),

    content              text not null,
    content_hash         text not null,

    -- Kind-specific fields. NULL when not applicable; the extractor
    -- writes JSON-typed values into `metadata` for less-common fields.
    owner                text,        -- for action_items
    entity_type          text,        -- for entities: "person"|"project"|"service"|...

    -- Traceability: every memory ALWAYS knows where it came from.
    source_kind          text not null
        check (source_kind in ('slack', 'gmail')),
    source_stable_key    text not null,
    source_timestamp     timestamptz,

    -- Bag of kind-specific extras. Always written by the extractor;
    -- never null. Empty `{}` for entities with no extras.
    metadata             jsonb not null default '{}'::jsonb,

    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

-- Idempotency anchor.
do $$
begin
    begin
        alter table public.extracted_memories
            add constraint extracted_memories_dedupe_key
            unique (workspace_id, kind, content_hash, source_stable_key);
    exception when others then
        null;   -- already exists
    end;
end$$;

-- The recall pipeline filters by (workspace_id, kind) and by source
-- traceability. Index those two narrow paths; everything else can
-- table-scan a workspace's small memory set.
create index if not exists extracted_memories_workspace_kind_idx
    on public.extracted_memories (workspace_id, kind);

create index if not exists extracted_memories_source_idx
    on public.extracted_memories (workspace_id, source_stable_key);

-- Refresh updated_at on every change.
drop trigger if exists extracted_memories_set_updated_at
    on public.extracted_memories;
create trigger extracted_memories_set_updated_at
    before update on public.extracted_memories
    for each row execute function public.set_updated_at();

-- RLS enabled, no policies -> service-role backend only.
alter table public.extracted_memories enable row level security;