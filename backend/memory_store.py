"""
Phase 12: persistence layer for extracted structured memory.

Wraps Supabase access to the `extracted_memories` table. All
operations are workspace-scoped; the table has RLS enabled with no
policies, so only this service-role backend can read or write rows.

Public API
----------
    persist_memories(workspace_id, source_kind, source_stable_key,
                     source_timestamp, memories) -> int

        Idempotent upsert. `memories` is the list of records returned
        by memory_extraction.extract_all(). Returns the count of
        records sent (the unique constraint on
        (workspace_id, kind, content_hash, source_stable_key) makes
        repeated calls a no-op).

    list_memories(workspace_id, *, kinds=None, query=None,
                  limit=20) -> List[Dict[str, Any]]

        Return the workspace's memory rows, optionally filtered by
        kind(s) and/or a query string (case-insensitive content
        match). Used by recall.prepare_recall_context to fold memories
        into the candidate set, and by the optional /api/memories
        route for future UI.

    delete_memories_by_source(workspace_id, source_stable_key) -> bool

        Drop every memory row that traces back to one source. Used
        when a source is re-ingested OR removed; not wired up in
        this phase but provided so the contract is complete.

Failure mode
------------
Every public function catches Supabase exceptions and returns a
sensible empty / False value with a structured warning log. Memory
persistence MUST NEVER block ingestion -- a Supabase outage degrades
the second-brain layer gracefully without breaking Slack/Gmail
ingest.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from logging_config import get_logger
from supabase_client import get_supabase

logger = get_logger(__name__)


_VALID_KINDS = ("action_item", "decision", "summary", "entity")


def persist_memories(
    *,
    workspace_id: str,
    source_kind: str,
    source_stable_key: str,
    source_timestamp: Optional[str],
    memories: List[Dict[str, Any]],
) -> int:
    """
    Upsert a batch of extracted memory records for one source.

    `source_kind` is the connector ("slack" / "gmail") -- NOT the
    memory's own kind. The latter lives in each record's `"kind"`
    field.

    `source_timestamp` is an ISO-8601 string, the original message
    or email timestamp. Stored on every row so the recall pipeline
    can rank older memories below newer ones (a 6-month-old decision
    still applies, but a fresh action item is more likely to be
    pending).

    Returns the number of rows sent to Supabase (NOT the number
    actually inserted; PostgREST doesn't reliably distinguish
    insert from no-op-conflict). Returns 0 on validation failure
    or Supabase error.
    """
    if not workspace_id or not source_stable_key or not memories:
        return 0
    if source_kind not in ("slack", "gmail"):
        return 0

    rows: List[Dict[str, Any]] = []
    for m in memories:
        kind = (m.get("kind") or "").strip()
        content = (m.get("content") or "").strip()
        content_hash = (m.get("content_hash") or "").strip()
        if kind not in _VALID_KINDS or not content or not content_hash:
            continue
        row: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "kind": kind,
            "content": content,
            "content_hash": content_hash,
            "source_kind": source_kind,
            "source_stable_key": source_stable_key,
            "metadata": m.get("metadata") or {},
        }
        if source_timestamp:
            row["source_timestamp"] = source_timestamp
        owner = m.get("owner")
        if owner:
            row["owner"] = owner
        entity_type = m.get("entity_type")
        if entity_type:
            row["entity_type"] = entity_type
        rows.append(row)

    if not rows:
        return 0

    try:
        client = get_supabase()
        client.table("extracted_memories").upsert(
            rows,
            on_conflict="workspace_id,kind,content_hash,source_stable_key",
            ignore_duplicates=False,
        ).execute()
    except Exception as e:  # noqa: BLE001
        # Same defensive policy as upsert_slack_channels: extract the
        # structured PostgREST body when possible so an operator can
        # see the cause without leaking row values.
        err_extra: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "source_kind": source_kind,
            "rows": len(rows),
            "error": type(e).__name__,
        }
        body = None
        try:
            body = e.json()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            body = None
        if isinstance(body, dict):
            for k in ("code", "message", "hint", "details"):
                v = body.get(k)
                if v:
                    err_extra[f"pg_{k}"] = str(v)[:300]
        logger.warning("memory_persist_failed", extra=err_extra)
        return 0
    return len(rows)


def list_memories(
    *,
    workspace_id: str,
    kinds: Optional[List[str]] = None,
    query: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Return the workspace's memory rows, optionally filtered by:

      - kinds:  list of "action_item" / "decision" / "summary" / "entity"
                (None = all kinds)
      - query:  case-insensitive substring match against `content`.
                The recall pipeline calls this with the user's question
                text to surface matching memories alongside the
                HydraDB chunks.

    Always workspace-scoped. Returns an empty list on Supabase error
    so callers can degrade gracefully.
    """
    if not workspace_id:
        return []
    safe_limit = max(1, min(int(limit or 20), 100))
    try:
        client = get_supabase()
        q = (
            client.table("extracted_memories")
            .select(
                "id, kind, content, owner, entity_type, source_kind, " "source_stable_key, source_timestamp, metadata"
            )
            .eq("workspace_id", workspace_id)
        )
        if kinds:
            safe_kinds = [k for k in kinds if k in _VALID_KINDS]
            if not safe_kinds:
                return []
            q = q.in_("kind", safe_kinds)
        if query and query.strip():
            # ilike with `%pattern%` is supabase-py's case-insensitive
            # substring match. PostgREST escapes special chars for us.
            q = q.ilike("content", f"%{query.strip()}%")
        # Newest sources first; falls back to created_at when a row's
        # source_timestamp is null.
        resp = (
            q.order(
                "source_timestamp",
                desc=True,
                nullsfirst=False,
            )
            .limit(safe_limit)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "memory_list_failed",
            extra={
                "workspace_id": workspace_id,
                "error": type(e).__name__,
            },
        )
        return []
    rows = getattr(resp, "data", None) or []
    return list(rows)


def delete_memories_by_source(
    *,
    workspace_id: str,
    source_stable_key: str,
) -> bool:
    """
    Drop every memory row for one source. Used when a source is
    re-ingested AND the caller wants to clear stale records before
    re-extraction; not wired up automatically (re-extraction normally
    just upserts on top of existing rows, which is fine since the
    unique constraint dedupes).
    """
    if not workspace_id or not source_stable_key:
        return False
    try:
        client = get_supabase()
        client.table("extracted_memories").delete().eq(
            "workspace_id",
            workspace_id,
        ).eq("source_stable_key", source_stable_key).execute()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "memory_delete_failed",
            extra={
                "workspace_id": workspace_id,
                "error": type(e).__name__,
            },
        )
        return False
    return True


# ---------------------------------------------------------------------- #
# Convenience: extract + persist in one call
# ---------------------------------------------------------------------- #


def extract_and_persist(
    *,
    workspace_id: str,
    source_kind: str,
    source_stable_key: str,
    source_timestamp: Optional[str],
    text: str,
    default_owner: Optional[str] = None,
) -> int:
    """
    Single-call helper used by the Slack + Gmail ingest paths.
    Extracts every memory kind from `text` and persists in one batch.
    Returns the number of records sent.

    Defensive: any failure inside extraction OR persistence is
    swallowed + logged so ingestion can never be blocked by the
    second-brain layer. Returns 0 on any error.
    """
    if not workspace_id or not source_stable_key or not text:
        return 0
    try:
        # Local import keeps the persistence layer importable even
        # when the extractor is being mocked at test time.
        from memory_extraction import extract_all  # noqa: PLC0415

        memories = extract_all(
            text,
            default_owner=default_owner,
            source_kind=source_kind,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "memory_extract_failed",
            extra={
                "workspace_id": workspace_id,
                "source_stable_key": source_stable_key,
                "error": type(e).__name__,
            },
        )
        return 0
    if not memories:
        return 0
    return persist_memories(
        workspace_id=workspace_id,
        source_kind=source_kind,
        source_stable_key=source_stable_key,
        source_timestamp=source_timestamp,
        memories=memories,
    )
