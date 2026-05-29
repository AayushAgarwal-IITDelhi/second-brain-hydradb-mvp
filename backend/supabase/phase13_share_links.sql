-- =============================================================================
-- Phase 13: shared_links — public read-only sharing of saved answers
-- =============================================================================
--
-- One row per shared saved-answer link. The `share_token` IS the
-- credential: a 32+ byte URL-safe random string minted by
-- secrets.token_urlsafe(32). The token's entropy alone authenticates
-- the public read endpoint -- no JWT, no auth header. Same model as
-- Notion / Figma "anyone with the link" sharing.
--
-- Security properties
-- --------------------
-- - The public read route NEVER returns workspace_id, created_by, or
--   any cross-record data. Only the saved-answer fields are exposed.
-- - Revoking a link sets `revoked_at`; the public route 404s revoked
--   tokens rather than 403ing them, so an attacker can't distinguish
--   "doesn't exist" from "revoked".
-- - Same for expired tokens (expires_at < now()).
-- - RLS enabled with NO policies -> service-role backend only. The
--   public read route on the backend uses service-role to bypass RLS
--   but still does ownership/expiry checks in Python before responding.
-- - Workspace isolation is preserved: a share link is bound to one
--   workspace_id at creation, and `saved_answer_id` must reference a
--   saved_answer in that same workspace (enforced by the create route).
--
-- Schema is intentionally tiny: no audit log, no rotation, no
-- permissions matrix. Phase 13 ships the simplest secure thing.
-- =============================================================================

create table if not exists public.shared_links (
    id                  uuid primary key default gen_random_uuid(),
    share_token         text not null unique,
    workspace_id        uuid not null references public.workspaces(id) on delete cascade,
    saved_answer_id     uuid not null,                  -- FK added below (idempotent)
    created_by          uuid not null,                  -- the user who shared it (auth.users.id)
    expires_at          timestamptz,                    -- null = no expiry
    revoked_at          timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

-- saved_answers might not exist at this exact name in older deploys;
-- add the FK defensively so the migration is safe to re-run + skips
-- gracefully when something upstream is off.
do $$
begin
    begin
        alter table public.shared_links
            add constraint shared_links_saved_answer_fk
            foreign key (saved_answer_id)
            references public.saved_answers(id)
            on delete cascade;
    exception when others then
        null;       -- constraint already exists OR saved_answers missing
    end;
end$$;

-- Lookup patterns:
--   - by token (public read; most frequent path)
--   - by (workspace_id, saved_answer_id) (admin "is this answer shared?")
--   - by created_by (admin "list my shares")
create unique index if not exists shared_links_token_idx
    on public.shared_links (share_token);

create index if not exists shared_links_workspace_idx
    on public.shared_links (workspace_id);

create index if not exists shared_links_saved_answer_idx
    on public.shared_links (workspace_id, saved_answer_id);

create index if not exists shared_links_created_by_idx
    on public.shared_links (created_by);

drop trigger if exists shared_links_set_updated_at
    on public.shared_links;
create trigger shared_links_set_updated_at
    before update on public.shared_links
    for each row execute function public.set_updated_at();

-- RLS enabled, no policies -> service-role backend only.
alter table public.shared_links enable row level security;