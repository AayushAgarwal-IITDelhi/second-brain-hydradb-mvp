"""
Gmail Connect (Phase 8) — OAuth state, code exchange, refresh, label /
message fetch, document builder, and the per-workspace ingestion
runner.

Single-module-per-connector mirrors the Slack module (slack_oauth.py)
on purpose:
    - same OAuth state pattern (HMAC-signed, nonce + expiry)
    - same "build_connect_url / exchange_code / run_*_ingest" surface
    - same callable signatures so the routes look symmetric

We deliberately use plain `requests` calls against the Gmail REST API
rather than google-api-python-client. That keeps the dependency
footprint minimal (no pyopenssl / grpc / oauthlib churn) and makes
tests trivial to mock — patch `requests.post` / `requests.get`.

Token security:
    - access_token + refresh_token live ONLY in gmail_connections
      (RLS denies all client reads; only the service-role backend
      can pull them).
    - Tokens are NEVER logged. Helpers redact them everywhere.
    - The frontend gets only the public projection (see
      supabase_client.get_gmail_connection_public).

Email-body privacy:
    - We log message counts, label IDs, and connection IDs.
    - We DO NOT log subjects, addresses, or body text. The dead-letter
      logger receives only counts + IDs.
"""

from __future__ import annotations

import base64
import html
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from logging_config import get_logger
from oauth_common import (
    make_oauth_state as _core_make_state,
    verify_oauth_state as _core_verify_state,
)
from observability import emit_dead_letter
from retry import retry_with_backoff

logger = get_logger(__name__)

# Minimal read-only Gmail scopes. We DO NOT request gmail.modify or
# gmail.send -- Phase 8 is read-only. openid + email + profile give us
# enough identity info to remember which Google account this is.
GMAIL_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
)


# Cap how many messages a single ingest run can pull. Defends against
# accidental whole-mailbox ingests. Operators can raise it via env.
def _max_messages_per_run() -> int:
    try:
        return max(1, int(os.getenv("GMAIL_MAX_MESSAGES_PER_RUN", "100")))
    except ValueError:
        return 100


# ---------------------------------------------------------------------- #
# Env access (helpers wrapped so tests can monkeypatch fresh values)
# ---------------------------------------------------------------------- #

def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _client_id() -> str:
    return _env("GMAIL_CLIENT_ID")


def _client_secret() -> str:
    return _env("GMAIL_CLIENT_SECRET")


def _redirect_uri() -> str:
    return _env("GMAIL_REDIRECT_URI")


def _state_secret() -> str:
    """
    HMAC key for OAuth state. Separate from SUPABASE_JWT_SECRET and
    SLACK_OAUTH_STATE_SECRET on purpose -- a leak of one doesn't
    compromise the others.
    """
    return _env("GMAIL_OAUTH_STATE_SECRET")


def gmail_oauth_configured() -> bool:
    """True iff all three Google OAuth env values are present."""
    return bool(_client_id() and _client_secret() and _redirect_uri())


# ---------------------------------------------------------------------- #
# OAuth state — HMAC-signed token binding workspace_id + user_id + nonce
# ---------------------------------------------------------------------- #
# Thin wrappers around oauth_common. The shared crypto lives there so
# a single fix applies to both Slack and Gmail; the connector-specific
# secret lookup and fail-closed guard stay here.

def make_oauth_state(workspace_id: str, user_id: str) -> str:
    """
    Build a tamper-evident state token for Google OAuth.

    Format: base64url(payload) "." base64url(signature)
    """
    secret = _state_secret()
    if not secret:
        raise RuntimeError("GMAIL_OAUTH_STATE_SECRET is not set.")
    return _core_make_state(secret, workspace_id, user_id)


def verify_oauth_state(state: str) -> Optional[Dict[str, Any]]:
    """
    Validate a state returned by Google. Returns the payload dict on
    success, None on any failure. Never raises -- callers branch on None.
    """
    return _core_verify_state(_state_secret(), state)


# ---------------------------------------------------------------------- #
# Connect-Gmail URL
# ---------------------------------------------------------------------- #

def build_connect_url(*, workspace_id: str, user_id: str) -> str:
    """
    Build the Google OAuth 2.0 authorize URL.

    Notes on params:
      - access_type=offline -> Google issues a refresh_token.
      - prompt=consent      -> Forces the consent screen so Google
                               re-issues the refresh_token on every
                               connect (otherwise re-connecting an
                               account returns NO refresh_token,
                               leaving us with a dead connection).
      - include_granted_scopes=true -> incremental auth, future-proof.
    """
    state = make_oauth_state(workspace_id, user_id)
    qs = urlencode({
        "client_id":               _client_id(),
        "redirect_uri":            _redirect_uri(),
        "response_type":           "code",
        "scope":                   " ".join(GMAIL_SCOPES),
        "access_type":             "offline",
        "prompt":                  "consent",
        "include_granted_scopes":  "true",
        "state":                   state,
    })
    return f"https://accounts.google.com/o/oauth2/v2/auth?{qs}"


# ---------------------------------------------------------------------- #
# OAuth code exchange + token refresh
# ---------------------------------------------------------------------- #

def exchange_code(code: str) -> Optional[Dict[str, Any]]:
    """
    Exchange an authorization code for an access/refresh token pair.

    Returns the parsed token response dict on success, None on failure.
    Never raises, never logs tokens.
    """
    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code":          code,
                "client_id":     _client_id(),
                "client_secret": _client_secret(),
                "redirect_uri":  _redirect_uri(),
                "grant_type":    "authorization_code",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        logger.warning(
            "gmail_oauth_exchange_request_failed",
            extra={"error": type(e).__name__},
        )
        return None

    if not resp.ok:
        logger.warning(
            "gmail_oauth_exchange_http_error",
            extra={"status": resp.status_code},
        )
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or "access_token" not in data:
        logger.warning("gmail_oauth_exchange_missing_token")
        return None
    return data


def refresh_access_token(refresh_token: str) -> Optional[Dict[str, Any]]:
    """
    Exchange a refresh_token for a fresh access_token.

    Returns the parsed response (which contains a new `access_token`
    and an `expires_in`) or None on failure. Google does NOT re-issue
    a refresh_token here -- the caller keeps the existing one.
    """
    if not refresh_token:
        return None
    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":     _client_id(),
                "client_secret": _client_secret(),
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        logger.warning(
            "gmail_oauth_refresh_request_failed",
            extra={"error": type(e).__name__},
        )
        return None

    if not resp.ok:
        logger.warning(
            "gmail_oauth_refresh_http_error",
            extra={"status": resp.status_code},
        )
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def fetch_user_info(access_token: str) -> Optional[Dict[str, Any]]:
    """
    Resolve the Google user's id + email using the userinfo endpoint.
    Required at callback time so we know which gmail_connections row
    to upsert into.
    """
    if not access_token:
        return None
    try:
        resp = requests.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.warning(
            "gmail_userinfo_request_failed",
            extra={"error": type(e).__name__},
        )
        return None
    if not resp.ok:
        logger.warning(
            "gmail_userinfo_http_error",
            extra={"status": resp.status_code},
        )
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def installation_from_token_response(
    token_resp: Dict[str, Any],
    user_info: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Project a Google token-exchange response + userinfo response into
    the row shape gmail_connections expects. Missing fields collapse
    to safe defaults so the upsert can still proceed.

    expiry_iso is set when `expires_in` is present, in UTC.
    """
    expires_in = token_resp.get("expires_in")
    expiry_iso: Optional[str] = None
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expiry = datetime.now(timezone.utc).timestamp() + int(expires_in)
        expiry_iso = datetime.fromtimestamp(
            expiry, tz=timezone.utc,
        ).isoformat()

    return {
        "google_user_id": (user_info.get("sub") or "").strip(),
        "email":          (user_info.get("email") or "").strip(),
        "access_token":   (token_resp.get("access_token") or "").strip(),
        "refresh_token":  (token_resp.get("refresh_token") or "").strip(),
        "scopes":         (token_resp.get("scope") or "").strip(),
        "token_expiry":   expiry_iso,
    }


# ---------------------------------------------------------------------- #
# Authenticated-Gmail calls — auto-refresh on 401
# ---------------------------------------------------------------------- #
# Every helper below routes through _authed_request so a single place
# handles "access token expired -> refresh -> retry". The refresh
# updates the in-memory `access_token` on the passed connection dict
# AND returns the new value so callers can persist it back.

class GmailApiError(Exception):
    """Raised by helpers when Gmail returns a permanent error."""


def _authed_request(
    method: str,
    url: str,
    connection: Dict[str, Any],
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 15,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Make a request to Gmail. Refresh the access_token once on 401 and
    retry. Returns (json, connection) where `connection` has been
    updated with the new access_token if a refresh occurred.

    Raises GmailApiError on persistent failure (so the ingest runner
    can dead-letter the job).
    """
    access_token = (connection.get("access_token") or "").strip()
    if not access_token:
        # No usable access token in memory; try to mint one before the call.
        refreshed = refresh_access_token(connection.get("refresh_token") or "")
        if not refreshed or "access_token" not in refreshed:
            raise GmailApiError("Could not obtain Gmail access token.")
        access_token = refreshed["access_token"]
        connection["access_token"] = access_token

    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = requests.request(
            method, url, headers=headers, params=params, timeout=timeout,
        )
    except requests.RequestException as e:
        raise GmailApiError(f"Gmail HTTP failed: {type(e).__name__}")

    if resp.status_code == 401:
        # Refresh and retry exactly once.
        refreshed = refresh_access_token(connection.get("refresh_token") or "")
        if not refreshed or "access_token" not in refreshed:
            raise GmailApiError("Gmail refresh failed (401).")
        access_token = refreshed["access_token"]
        connection["access_token"] = access_token
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            resp = requests.request(
                method, url, headers=headers, params=params, timeout=timeout,
            )
        except requests.RequestException as e:
            raise GmailApiError(f"Gmail HTTP retry failed: {type(e).__name__}")

    if resp.status_code == 429 or 500 <= resp.status_code < 600:
        # Transient: the retry layer above us can re-call.
        raise GmailApiError(f"Gmail transient HTTP {resp.status_code}")
    if not resp.ok:
        raise GmailApiError(f"Gmail HTTP {resp.status_code}")

    try:
        return resp.json(), connection
    except ValueError:
        raise GmailApiError("Gmail returned non-JSON response.")


def list_labels(connection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Return every label visible to this Gmail account.

    Shape:
        [{"label_id": "Label_1", "name": "Updates", "type": "user"}, ...]
    """
    data, _conn = _authed_request(
        "GET",
        "https://gmail.googleapis.com/gmail/v1/users/me/labels",
        connection,
    )
    out: List[Dict[str, Any]] = []
    for row in (data or {}).get("labels") or []:
        lid = (row.get("id") or "").strip()
        if not lid:
            continue
        out.append({
            "label_id": lid,
            "name":     (row.get("name") or "").strip(),
            "type":     (row.get("type") or "user").strip(),
        })
    return out


def list_message_ids_for_label(
    connection: Dict[str, Any],
    label_id: str,
    *,
    max_results: int = 100,
) -> List[str]:
    """
    Return the most recent message IDs for a label. `max_results` is
    capped at GMAIL_MAX_MESSAGES_PER_RUN by the runner; we honor whatever
    the caller passes here so unit tests can use small numbers.
    """
    ids: List[str] = []
    page_token: Optional[str] = None
    while len(ids) < max_results:
        params: Dict[str, Any] = {
            "labelIds":   label_id,
            "maxResults": min(100, max_results - len(ids)),
        }
        if page_token:
            params["pageToken"] = page_token
        data, _conn = _authed_request(
            "GET",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            connection,
            params=params,
        )
        for row in (data or {}).get("messages") or []:
            mid = (row.get("id") or "").strip()
            if mid:
                ids.append(mid)
        page_token = (data or {}).get("nextPageToken")
        if not page_token:
            break
    return ids[:max_results]


def fetch_message(
    connection: Dict[str, Any], message_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Fetch a single message with `format=full` so we get headers + body.
    Returns the Gmail message dict, or None on permanent failure.
    """
    try:
        data, _conn = _authed_request(
            "GET",
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
            connection,
            params={"format": "full"},
        )
    except GmailApiError as e:
        logger.warning(
            "gmail_fetch_message_failed",
            extra={"message_id": message_id, "error": str(e)},
        )
        return None
    return data


# ---------------------------------------------------------------------- #
# Message -> Markdown
# ---------------------------------------------------------------------- #

def _decode_b64url(s: str) -> str:
    if not s:
        return ""
    padding = "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s + padding).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _extract_text_from_payload(payload: Dict[str, Any]) -> str:
    """
    Walk a Gmail message payload tree and return the first text/plain
    body we find. If only text/html is present anywhere in the tree,
    strip the HTML tags minimally so the recall pipeline gets readable
    text.

    Two passes through the tree so a text/plain part wins over a
    text/html sibling regardless of which appears first. Real
    multipart/alternative payloads list HTML before plain, and we
    don't want a leading html part to short-circuit the search.
    """
    if not isinstance(payload, dict):
        return ""

    plain = _find_part_text(payload, "text/plain")
    if plain:
        return plain
    html_text = _find_part_text(payload, "text/html")
    if html_text:
        return _strip_html(html_text)
    return ""


def _find_part_text(payload: Dict[str, Any], wanted_mime: str) -> str:
    """
    Depth-first search for the first body data with mimeType == `wanted_mime`.
    Returns the decoded text, or "" if no such part exists.
    """
    if not isinstance(payload, dict):
        return ""

    mime = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    data = body.get("data") or ""

    if mime == wanted_mime and data:
        return _decode_b64url(data)

    for child in payload.get("parts") or []:
        text = _find_part_text(child, wanted_mime)
        if text:
            return text
    return ""


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(raw_html: str) -> str:
    """Remove tags, decode entities, collapse whitespace. Very basic."""
    if not raw_html:
        return ""
    no_tags = _HTML_TAG_RE.sub(" ", raw_html)
    decoded = html.unescape(no_tags)
    return _WHITESPACE_RE.sub(" ", decoded).strip()


def _header_value(headers: List[Dict[str, str]], name: str) -> str:
    name_lower = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == name_lower:
            return (h.get("value") or "").strip()
    return ""


def stable_key_for_gmail_message(message_id: str) -> str:
    """
    Stable, unique key for HydraDB dedupe. Gmail's `id` is globally
    unique across mailboxes, so we don't need to include workspace_id.
    """
    return f"gmail:msg:{message_id}"


def _safe_filename_part(s: str, max_len: int = 40) -> str:
    """Filename-safe slug (matches the Slack ingestion approach)."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s or "").strip("_")
    return s[:max_len] or "x"


def _truncate(s: str, max_len: int) -> str:
    s = s or ""
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def build_email_document(
    message: Dict[str, Any],
    connection_email: str,
) -> Optional[Dict[str, Any]]:
    """
    Convert a Gmail message dict into the {filename, content, stable_key,
    ...} shape HydraDBClient.upload_knowledge expects. Returns None when
    the message has no usable text (e.g. an empty receipt that's just
    images).

    DOES NOT log any header values or body text -- only counts / IDs
    flow into logs from here.
    """
    if not isinstance(message, dict):
        return None
    message_id = (message.get("id") or "").strip()
    if not message_id:
        return None

    payload = message.get("payload") or {}
    headers = payload.get("headers") or []
    subject = _header_value(headers, "Subject") or "(no subject)"
    sender  = _header_value(headers, "From")
    to      = _header_value(headers, "To")
    cc      = _header_value(headers, "Cc")
    date    = _header_value(headers, "Date")
    snippet = (message.get("snippet") or "").strip()
    label_ids = message.get("labelIds") or []
    body_text = _extract_text_from_payload(payload).strip()

    if not body_text and not snippet:
        # Nothing to index; skip silently.
        return None

    stable_key = stable_key_for_gmail_message(message_id)
    # Gmail web client deep link. Always works for the mailbox owner.
    permalink = (
        f"https://mail.google.com/mail/u/0/#all/{message_id}"
        if message_id else None
    )

    # Build the header block. `Cc:` is only emitted when present so we
    # don't pollute every email doc with a blank line.
    header_lines = [
        "# Email",
        f"Source Key: {stable_key}",
        f"Message-Id: {message_id}",
        f"Mailbox: {connection_email}",
        f"Subject: {_truncate(subject, 200)}",
        f"From: {_truncate(sender, 200)}",
        f"To: {_truncate(to, 200)}",
    ]
    if cc:
        header_lines.append(f"Cc: {_truncate(cc, 200)}")
    header_lines.extend([
        f"Date: {date}",
        f"Labels: {', '.join(label_ids)}",
        f"Snippet: {_truncate(snippet, 280)}",
    ])
    if permalink:
        header_lines.append(f"Permalink: {permalink}")

    # Cap the body at 32k chars. Real emails rarely exceed this; if one
    # does we'd rather index a meaningful prefix than refuse the doc.
    body_for_doc = _truncate(body_text or snippet, 32_000)
    content = "\n".join(header_lines + ["", body_for_doc])

    filename = f"gmail_{_safe_filename_part(message_id)}.md"
    return {
        "filename":      filename,
        "content":       content,
        "stable_key":    stable_key,
        # Extra metadata that HydraDB / state.mark_uploaded carry forward.
        # We intentionally do NOT include the subject or body here --
        # only IDs, so a state.json leak doesn't expose mail content.
        "message_id":    message_id,
        "document_type": "email",
        "snippet":       _truncate(snippet, 280),
        "permalink":     permalink,
    }


# ---------------------------------------------------------------------- #
# Per-workspace ingestion runner
# ---------------------------------------------------------------------- #
# Synchronous on purpose: the caller wires this into a FastAPI
# BackgroundTask so the HTTP request returns immediately and the heavy
# lifting happens in the worker. Mirrors slack_oauth.run_workspace_ingest.

def run_workspace_gmail_ingest(
    *,
    workspace_id: str,
    connection: Dict[str, Any],
    label_ids: List[str],
    hydradb_sub_tenant_id: Optional[str] = None,
    max_messages: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Ingest the most recent messages from each selected label into the
    workspace's HydraDB sub-tenant. Returns a small stats dict.

    Behavior:
      - Caps the total messages this run will pull at GMAIL_MAX_MESSAGES_PER_RUN
        (or the explicit `max_messages` override). The cap is shared
        across all labels in this call.
      - Skips SPAM and TRASH labels even if explicitly selected, unless
        the env GMAIL_ALLOW_SPAM_TRASH=true. Defensive default.
      - On any permanent error per-label, emits a dead_letter event
        but continues with other labels.
      - Updates gmail_ingestion_state.last_synced_at for every label
        we successfully processed (even if zero messages came back).

    The function returns a stats dict; it never raises to the caller.
    """
    from hydradb_client import HydraDBClient, summarize_upload_response  # noqa: PLC0415
    from supabase_client import upsert_gmail_ingestion_state              # noqa: PLC0415

    summary: Dict[str, Any] = {
        "labels_processed":   0,
        "labels_skipped":     0,
        "labels_failed":      0,
        "messages_fetched":   0,
        "messages_uploaded":  0,
        "messages_failed":    0,
        "messages_skipped":   0,
    }
    if not label_ids:
        return summary

    refresh_token = (connection.get("refresh_token") or "").strip()
    if not refresh_token:
        emit_dead_letter(
            kind="gmail_ingest",
            workspace_id=workspace_id,
            error=RuntimeError("missing_refresh_token"),
            context={"connection_id": connection.get("id")},
        )
        return summary

    cap_total = max_messages if max_messages is not None else _max_messages_per_run()
    cap_total = max(1, int(cap_total))
    allow_spam_trash = (
        os.getenv("GMAIL_ALLOW_SPAM_TRASH", "").strip().lower()
        in ("1", "true", "yes", "on")
    )

    if hydradb_sub_tenant_id:
        hydra = HydraDBClient(sub_tenant_id=hydradb_sub_tenant_id)
    else:
        logger.warning(
            "gmail_ingest_no_sub_tenant",
            extra={"workspace_id": workspace_id},
        )
        hydra = HydraDBClient()

    connection_id = connection.get("id")
    connection_email = (connection.get("email") or "").strip()

    logger.info(
        "gmail_ingest_start",
        extra={
            "workspace_id":  workspace_id,
            "connection_id": connection_id,
            "label_count":   len(label_ids),
            "cap_total":     cap_total,
        },
    )

    remaining = cap_total
    for label_id in label_ids:
        if remaining <= 0:
            summary["labels_skipped"] += 1
            continue

        # Spam/trash safety guard.
        if not allow_spam_trash and label_id in ("SPAM", "TRASH"):
            logger.info(
                "gmail_ingest_label_blocked",
                extra={
                    "workspace_id":  workspace_id,
                    "connection_id": connection_id,
                    "label_id":      label_id,
                    "reason":        "spam_or_trash",
                },
            )
            summary["labels_skipped"] += 1
            continue

        try:
            message_ids = retry_with_backoff(
                list_message_ids_for_label,
                connection, label_id,
                max_results=min(remaining, 100),
                attempts=3,
                initial_delay=0.5,
                max_delay=4.0,
                retry_on=(GmailApiError,),
                op_name="gmail_list_messages",
            )
        except GmailApiError as e:
            summary["labels_failed"] += 1
            emit_dead_letter(
                kind="gmail_ingest_label",
                workspace_id=workspace_id,
                error=e,
                context={
                    "connection_id": connection_id,
                    "label_id":      label_id,
                    "stage":         "list_messages",
                },
            )
            continue

        prepared: List[Dict[str, Any]] = []
        for mid in message_ids:
            if remaining <= 0:
                break
            try:
                msg = retry_with_backoff(
                    fetch_message,
                    connection, mid,
                    attempts=2,
                    initial_delay=0.5,
                    max_delay=2.0,
                    retry_on=(GmailApiError,),
                    op_name="gmail_fetch_message",
                )
            except GmailApiError:
                summary["messages_failed"] += 1
                continue
            if not msg:
                summary["messages_failed"] += 1
                continue
            doc = build_email_document(msg, connection_email)
            if doc is None:
                summary["messages_skipped"] += 1
                continue
            prepared.append(doc)
            summary["messages_fetched"] += 1
            remaining -= 1

        if prepared:
            try:
                response = hydra.upload_knowledge(prepared)
            except Exception as e:  # noqa: BLE001
                emit_dead_letter(
                    kind="gmail_ingest_upload",
                    workspace_id=workspace_id,
                    error=e,
                    context={
                        "connection_id": connection_id,
                        "label_id":      label_id,
                        "file_count":    len(prepared),
                    },
                )
                summary["messages_failed"] += len(prepared)
                summary["labels_failed"] += 1
                continue
            ok, _bad = summarize_upload_response(
                response if isinstance(response, dict) else {},
                batch_size=len(prepared),
            )
            summary["messages_uploaded"] += ok
            summary["messages_failed"] += max(0, len(prepared) - ok)

        # Always touch the ingestion-state row so an operator can see
        # which labels are warm, even if zero messages came back.
        try:
            upsert_gmail_ingestion_state(
                workspace_id=workspace_id,
                gmail_connection_id=connection_id,
                label_id=label_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "gmail_ingestion_state_update_failed",
                extra={
                    "workspace_id":  workspace_id,
                    "connection_id": connection_id,
                    "label_id":      label_id,
                    "error":         type(e).__name__,
                },
            )

        summary["labels_processed"] += 1

    logger.info(
        "gmail_ingest_complete",
        extra={
            "workspace_id":  workspace_id,
            "connection_id": connection_id,
            **summary,
        },
    )
    return summary