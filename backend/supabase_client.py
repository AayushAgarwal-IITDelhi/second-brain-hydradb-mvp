"""
Server-side Supabase client for Phase 1 workspace lookups.

Uses the official `supabase` Python client with the project's
service_role key so it bypasses row-level security — needed for
membership checks on routes the user has not yet been authorized for.

Service-role credentials MUST NEVER reach the frontend. They belong in
the backend .env only.

Phase 1 surface:
    get_workspace_membership(user_id, workspace_id) -> role | None
    list_user_workspaces(user_id)                   -> list[dict]

Errors are swallowed and logged — they translate to "no membership" /
empty list on the caller side. The caller decides how to surface that
(403 for require_workspace, empty UI for the workspaces listing).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Dict, List, Optional

from supabase import Client, create_client

from logging_config import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """
    Build and cache a Supabase client at first use.

    Lazy: importing this module does NOT touch the env or open a
    connection. The client is materialized only when something asks
    for it. This keeps the smoke import check happy in environments
    where Supabase credentials aren't set.
    """
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set."
        )
    return create_client(url, key)


def reset_supabase_cache() -> None:
    """Test hook: clear the cached client so env changes take effect."""
    get_supabase.cache_clear()


def get_workspace_membership(
    *, user_id: str, workspace_id: str
) -> Optional[str]:
    """
    Return the role ('owner' | 'admin' | 'member') if the user is a
    member of the workspace, or None otherwise.

    Any error (network, schema, etc.) is logged and treated as "no
    membership". We deliberately do NOT propagate DB error detail to
    the caller — the caller only needs the access verdict.
    """
    try:
        client = get_supabase()
        resp = (
            client.table("workspace_members")
            .select("role")
            .eq("user_id", user_id)
            .eq("workspace_id", workspace_id)
            .limit(1)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_membership_lookup_failed',
            extra={
                'user_id':      user_id,
                'workspace_id': workspace_id,
                'error':        type(e).__name__,
            },
        )
        return None

    rows = getattr(resp, "data", None) or []
    if not rows:
        return None

    role = rows[0].get("role")
    # Defensive: PostgREST should always return a non-empty role since the
    # column is NOT NULL, but if a schema change ever produces a null we'd
    # rather refuse access than crash the request.
    if not isinstance(role, str) or not role.strip():
        return None
    return role


def list_user_workspaces(*, user_id: str) -> List[Dict[str, Any]]:
    """
    Return the workspaces the user belongs to, with their role.

    Shape:
        [
            {"id": str, "name": str, "slug": str, "role": str},
            ...
        ]

    Order is whatever PostgREST returns (insertion order for now). Errors
    return an empty list — the frontend handles "no workspaces yet" the
    same way regardless of cause.
    """
    try:
        client = get_supabase()
        resp = (
            client.table("workspace_members")
            .select("role, workspace:workspaces(id, name, slug)")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_list_workspaces_failed',
            extra={'user_id': user_id, 'error': type(e).__name__},
        )
        return []

    out: List[Dict[str, Any]] = []
    for row in getattr(resp, "data", []) or []:
        ws = row.get("workspace") or {}
        ws_id = ws.get("id")
        if not ws_id:
            continue
        out.append({
            "id":   ws_id,
            "name": ws.get("name") or "",
            "slug": ws.get("slug") or "",
            "role": row.get("role") or "member",
        })
    return out


# =====================================================================
# Phase 2: chat sessions, chat messages, saved answers.
# =====================================================================
# All of these run via the service-role client (RLS bypassed) and ALWAYS
# scope by workspace_id + user_id explicitly. The HTTP layer
# (require_workspace) has already verified the caller is a member of
# the workspace; these helpers don't re-check, they just trust the
# arguments. The database RLS policies are defense-in-depth for the day
# something other than the backend talks to the DB.


# ---------- chat_sessions --------------------------------------------- #

def list_chat_sessions(
    *, workspace_id: str, user_id: str, limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Return the caller's chat sessions in this workspace, newest first.

    We scope to the caller's own sessions (user_id) rather than the whole
    workspace — chat threads are personal even inside a shared workspace.
    Saved answers (below) are shared because they're explicit bookmarks.
    """
    try:
        client = get_supabase()
        resp = (
            client.table("chat_sessions")
            .select("id, title, created_at, updated_at")
            .eq("workspace_id", workspace_id)
            .eq("user_id", user_id)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_list_sessions_failed',
            extra={
                'workspace_id': workspace_id,
                'user_id':      user_id,
                'error':        type(e).__name__,
            },
        )
        return []
    return list(getattr(resp, "data", []) or [])


def create_chat_session(
    *, workspace_id: str, user_id: str, title: str,
) -> Optional[Dict[str, Any]]:
    """
    Insert a new chat session row. Returns the inserted row or None on
    failure. Title is trimmed and capped here so callers don't have to.
    """
    safe_title = (title or "").strip() or "New chat"
    if len(safe_title) > 200:
        safe_title = safe_title[:200]
    try:
        client = get_supabase()
        resp = (
            client.table("chat_sessions")
            .insert({
                "workspace_id": workspace_id,
                "user_id":      user_id,
                "title":        safe_title,
            })
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_create_session_failed',
            extra={
                'workspace_id': workspace_id,
                'user_id':      user_id,
                'error':        type(e).__name__,
            },
        )
        return None
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


def get_chat_session(
    *, session_id: str, workspace_id: str, user_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Fetch a single session by id, scoped to (workspace_id, user_id) so
    you can't read another user's session inside your workspace, nor
    cross workspaces. Returns None if not found / not accessible.
    """
    try:
        client = get_supabase()
        resp = (
            client.table("chat_sessions")
            .select("id, title, created_at, updated_at")
            .eq("id", session_id)
            .eq("workspace_id", workspace_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_get_session_failed',
            extra={
                'session_id':   session_id,
                'workspace_id': workspace_id,
                'user_id':      user_id,
                'error':        type(e).__name__,
            },
        )
        return None
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


# ---------- chat_messages --------------------------------------------- #

_ALLOWED_MESSAGE_ROLES = ("user", "assistant")


def list_chat_messages(
    *, session_id: str, workspace_id: str, user_id: str,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """
    Return the messages in a session, oldest first.

    We re-confirm the session belongs to the caller before reading
    messages so a stolen session_id from another user can't be used to
    drain their chat. The call costs one extra round-trip but is cheap.
    """
    if not get_chat_session(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
    ):
        return []
    try:
        client = get_supabase()
        resp = (
            client.table("chat_messages")
            .select("id, role, content, sources, created_at")
            .eq("session_id", session_id)
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_list_messages_failed',
            extra={
                'session_id':   session_id,
                'workspace_id': workspace_id,
                'user_id':      user_id,
                'error':        type(e).__name__,
            },
        )
        return []
    return list(getattr(resp, "data", []) or [])


def create_chat_message(
    *, session_id: str, workspace_id: str, user_id: str,
    role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Append a message to a session. The caller must own the session — we
    verify that here so a forged session_id can't be used to write into
    another user's thread. Touches updated_at on the session so the
    sessions list re-orders correctly.
    """
    if role not in _ALLOWED_MESSAGE_ROLES:
        logger.warning(
            'supabase_create_message_bad_role',
            extra={'role': role, 'user_id': user_id},
        )
        return None
    if not get_chat_session(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
    ):
        return None
    try:
        client = get_supabase()
        resp = (
            client.table("chat_messages")
            .insert({
                "session_id":   session_id,
                "workspace_id": workspace_id,
                "user_id":      user_id,
                "role":         role,
                "content":      content or "",
                "sources":      sources if sources is not None else None,
            })
            .execute()
        )
        # Touch the session's updated_at so it floats to the top of the list.
        client.table("chat_sessions").update(
            {"updated_at": "now()"}
        ).eq("id", session_id).execute()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_create_message_failed',
            extra={
                'session_id':   session_id,
                'workspace_id': workspace_id,
                'user_id':      user_id,
                'error':        type(e).__name__,
            },
        )
        return None
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


# ---------- saved_answers --------------------------------------------- #

def list_saved_answers(
    *, workspace_id: str, user_id: str, limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    List saved answers in the workspace, newest first.

    Scoped to the caller's own saves (user_id) by default — that matches
    the previous localStorage semantics (each user only saw their own
    bookmarks). RLS would also allow other workspace members to read
    each other's saves; we deliberately filter to user_id here to avoid
    silently exposing a teammate's saved-answer list when nobody asked
    for it. Sharing-across-members is a follow-on UX decision.
    """
    try:
        client = get_supabase()
        resp = (
            client.table("saved_answers")
            .select(
                "id, question, answer, sources, mode, filters, debug, "
                "created_at"
            )
            .eq("workspace_id", workspace_id)
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_list_saved_failed',
            extra={
                'workspace_id': workspace_id,
                'user_id':      user_id,
                'error':        type(e).__name__,
            },
        )
        return []
    return list(getattr(resp, "data", []) or [])


def create_saved_answer(
    *,
    workspace_id: str,
    user_id: str,
    question: str,
    answer: str,
    sources: Optional[List[Dict[str, Any]]] = None,
    mode: Optional[str] = None,
    filters: Optional[Dict[str, Any]] = None,
    debug: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Insert a saved-answer row. Returns the inserted row (with its new id
    and created_at) or None on failure.
    """
    row = {
        "workspace_id": workspace_id,
        "user_id":      user_id,
        "question":     (question or "")[:5000],
        "answer":       answer or "",
        "sources":      sources if sources is not None else None,
        "mode":         (mode or None),
        "filters":      filters if filters is not None else None,
        "debug":        debug if debug is not None else None,
    }
    try:
        client = get_supabase()
        resp = (
            client.table("saved_answers").insert(row).execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_create_saved_failed',
            extra={
                'workspace_id': workspace_id,
                'user_id':      user_id,
                'error':        type(e).__name__,
            },
        )
        return None
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


def delete_saved_answer(
    *, saved_id: str, workspace_id: str, user_id: str,
) -> bool:
    """
    Delete a saved answer the caller owns. Scoped by workspace_id +
    user_id so a stolen id from a teammate's workspace can't be used to
    delete their bookmarks. Returns True when at least one row was
    removed.
    """
    try:
        client = get_supabase()
        resp = (
            client.table("saved_answers")
            .delete()
            .eq("id", saved_id)
            .eq("workspace_id", workspace_id)
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_delete_saved_failed',
            extra={
                'saved_id':     saved_id,
                'workspace_id': workspace_id,
                'user_id':      user_id,
                'error':        type(e).__name__,
            },
        )
        return False
    return bool(getattr(resp, "data", []) or [])

# =====================================================================
# Phase 3: Slack installations + Slack channels.
# =====================================================================
# Bot tokens (the most sensitive piece of data this app stores so far)
# live in slack_installations.bot_token. RLS denies all authenticated
# access to that table; only the service-role client used here can read
# it. We never serialize bot_token in any HTTP response — see main.py.


# ---------- slack_installations -------------------------------------- #

def upsert_slack_installation(
    *,
    workspace_id: str,
    slack_team_id: str,
    slack_team_name: str,
    bot_user_id: str,
    bot_token: str,
    scopes: str,
) -> Optional[Dict[str, Any]]:
    """
    Insert or update the workspace's Slack installation row. Returns the
    row on success, None on failure. Idempotent on workspace_id.
    """
    payload = {
        "workspace_id":    workspace_id,
        "slack_team_id":   slack_team_id,
        "slack_team_name": slack_team_name,
        "bot_user_id":     bot_user_id,
        "bot_token":       bot_token,
        "scopes":          scopes,
    }
    try:
        client = get_supabase()
        # `upsert` with on_conflict=workspace_id matches the UNIQUE
        # constraint declared in phase3_slack_connect.sql, so re-running
        # Connect for the same workspace updates the existing row.
        resp = (
            client.table("slack_installations")
            .upsert(payload, on_conflict="workspace_id")
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_upsert_installation_failed',
            extra={'workspace_id': workspace_id, 'error': type(e).__name__},
        )
        return None
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


def get_slack_installation(
    *, workspace_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Return the installation row for a workspace, including bot_token.
    Caller MUST keep bot_token server-side — never return it from an
    API endpoint.
    """
    try:
        client = get_supabase()
        resp = (
            client.table("slack_installations")
            .select("*")
            .eq("workspace_id", workspace_id)
            .limit(1)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_get_installation_failed',
            extra={'workspace_id': workspace_id, 'error': type(e).__name__},
        )
        return None
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


def get_slack_installation_public(
    *, workspace_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Same as get_slack_installation but WITHOUT bot_token, suitable for
    sending to the frontend. The presence of a return value means
    "Slack is connected for this workspace" — the frontend uses that
    to swap the Connect button for the channel picker.
    """
    row = get_slack_installation(workspace_id=workspace_id)
    if not row:
        return None
    return {
        "slack_team_id":   row.get("slack_team_id"),
        "slack_team_name": row.get("slack_team_name"),
        "bot_user_id":     row.get("bot_user_id"),
        "scopes":          row.get("scopes"),
        "connected_at":    row.get("created_at"),
        "updated_at":      row.get("updated_at"),
    }


# ---------- slack_channels ------------------------------------------- #

def upsert_slack_channels(
    *,
    workspace_id: str,
    channels: List[Dict[str, Any]],
) -> int:
    """
    Upsert a batch of channels for a workspace. Each item must have at
    least `slack_channel_id` and `name`; `is_archived` defaults to False.
    Existing rows are updated in place — selection state (is_selected)
    is NOT clobbered here, only name + is_archived are refreshed.

    Returns the number of rows touched (best-effort; PostgREST doesn't
    distinguish insert from update in its response count).
    """
    if not channels:
        return 0

    rows: List[Dict[str, Any]] = []
    for c in channels:
        cid = (c.get("slack_channel_id") or "").strip()
        if not cid:
            continue
        rows.append({
            "workspace_id":     workspace_id,
            "slack_channel_id": cid,
            "name":             (c.get("name") or "").strip(),
            "is_archived":      bool(c.get("is_archived")),
        })
    if not rows:
        return 0

    try:
        client = get_supabase()
        # ignore_duplicates=False so an existing row gets its name +
        # is_archived refreshed if Slack tells us they changed.
        resp = (
            client.table("slack_channels")
            .upsert(rows, on_conflict="workspace_id,slack_channel_id",
                    ignore_duplicates=False)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_upsert_channels_failed',
            extra={'workspace_id': workspace_id, 'error': type(e).__name__,
                   'count': len(rows)},
        )
        return 0
    return len(getattr(resp, "data", []) or [])


def list_workspace_channels(
    *, workspace_id: str,
) -> List[Dict[str, Any]]:
    """
    Return every known channel for a workspace, ordered by name.
    Includes selection state so the picker can hydrate checkboxes.
    """
    try:
        client = get_supabase()
        resp = (
            client.table("slack_channels")
            .select("slack_channel_id, name, is_selected, is_archived, "
                    "updated_at")
            .eq("workspace_id", workspace_id)
            .order("name", desc=False)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_list_channels_failed',
            extra={'workspace_id': workspace_id, 'error': type(e).__name__},
        )
        return []
    return list(getattr(resp, "data", []) or [])


def list_selected_channel_ids(
    *, workspace_id: str,
) -> List[str]:
    """
    Return just the channel IDs the workspace has marked for ingestion.
    Convenience for the ingest route — no point pulling unselected rows.
    """
    try:
        client = get_supabase()
        resp = (
            client.table("slack_channels")
            .select("slack_channel_id")
            .eq("workspace_id", workspace_id)
            .eq("is_selected", True)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_list_selected_channels_failed',
            extra={'workspace_id': workspace_id, 'error': type(e).__name__},
        )
        return []
    out: List[str] = []
    for row in getattr(resp, "data", []) or []:
        cid = (row.get("slack_channel_id") or "").strip()
        if cid:
            out.append(cid)
    return out


def set_selected_channels(
    *, workspace_id: str, selected_ids: List[str],
) -> bool:
    """
    Replace the selected set. Sets is_selected=true on every row whose
    slack_channel_id appears in `selected_ids`, and is_selected=false
    on every other row in the workspace. Channel rows must already exist
    (upserted by the previous /api/slack/channels GET) — this function
    intentionally does NOT create missing rows.

    Returns True on success, False on any failure.
    """
    try:
        client = get_supabase()

        # Two queries, one for each value of is_selected. We do this
        # explicitly rather than via two SQL UPDATE statements because
        # PostgREST doesn't support a single "set X to value Y where in
        # set / else value Z" expression. Each branch is constant-time
        # in row count so even hundreds of channels stay snappy.
        if selected_ids:
            client.table("slack_channels").update(
                {"is_selected": True}
            ).eq("workspace_id", workspace_id).in_(
                "slack_channel_id", selected_ids,
            ).execute()
            # Unselect everything NOT in the set.
            client.table("slack_channels").update(
                {"is_selected": False}
            ).eq("workspace_id", workspace_id).not_.in_(
                "slack_channel_id", selected_ids,
            ).execute()
        else:
            # Empty selection — unselect everything.
            client.table("slack_channels").update(
                {"is_selected": False}
            ).eq("workspace_id", workspace_id).execute()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            'supabase_set_selected_channels_failed',
            extra={'workspace_id': workspace_id, 'error': type(e).__name__,
                   'selected_count': len(selected_ids)},
        )
        return False
    return True