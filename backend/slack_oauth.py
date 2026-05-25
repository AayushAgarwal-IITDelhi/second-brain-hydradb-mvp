"""
Slack Connect (Phase 3) — OAuth state, OAuth code exchange, and the
per-workspace ingestion runner.

This module deliberately keeps everything related to Slack-OAuth-by-
workspace in one place so Phase 3 can be reviewed (and rolled back) as
a single unit. It depends only on:
    - the Slack Web API (via slack_sdk's WebClient)
    - supabase_client.py (for installation + channel CRUD)
    - existing ingestion primitives (SlackClientWrapper, process_channel,
      upload_in_batches, IngestionState) — reused unchanged so we don't
      diverge from the prototype's ingestion behavior.

We do NOT touch ingest_slack.main() — it still works as the env-driven
prototype CLI. The per-workspace flow re-uses the same primitives with
an explicit token + channel list instead of reading them from env.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from logging_config import get_logger

logger = get_logger(__name__)


# OAuth state lifetime. Five minutes is more than enough for a user to
# tap Connect, finish the Slack consent screen, and be redirected back.
STATE_LIFETIME_SECONDS = 300

# Default scopes we request from Slack. Bots need channels:history /
# groups:history to read messages, *:read to enumerate channels, and
# users:read so display names can be resolved during ingestion. Kept
# minimal — no posting, no DMs.
DEFAULT_SCOPES = (
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "users:read",
)


# ---------------------------------------------------------------------- #
# Env access (helpers wrapped so tests can monkeypatch without import-
# time evaluation freezing the values).
# ---------------------------------------------------------------------- #

def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _client_id() -> str:
    return _env("SLACK_CLIENT_ID")


def _client_secret() -> str:
    return _env("SLACK_CLIENT_SECRET")


def _redirect_uri() -> str:
    return _env("SLACK_REDIRECT_URI")


def _state_secret() -> str:
    """
    Resolve the HMAC key used to sign OAuth state. We deliberately keep
    this separate from SUPABASE_JWT_SECRET so a leak of one doesn't
    compromise the other.
    """
    return _env("SLACK_OAUTH_STATE_SECRET")


def slack_oauth_configured() -> bool:
    """True iff all three OAuth env values are present."""
    return bool(_client_id() and _client_secret() and _redirect_uri())


# ---------------------------------------------------------------------- #
# OAuth state — HMAC-signed token binding workspace_id + user_id + nonce
# ---------------------------------------------------------------------- #
# We don't want to persist state in a DB row per OAuth attempt — too
# much bookkeeping for a transient 5-minute window. Instead we sign a
# small payload, send it as ?state=..., and verify it on callback. The
# signing key (SLACK_OAUTH_STATE_SECRET) must be set; a missing key
# fails closed.

def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def make_oauth_state(workspace_id: str, user_id: str) -> str:
    """
    Build a tamper-evident state token binding this OAuth attempt to a
    specific workspace + user. Includes a short expiry so a stolen state
    can't be replayed forever, and a random nonce so two consecutive
    calls produce different tokens.

    Format: base64url(payload) "." base64url(signature)
    """
    secret = _state_secret()
    if not secret:
        # Fail closed — if we can't sign, we can't safely issue states.
        raise RuntimeError("SLACK_OAUTH_STATE_SECRET is not set.")

    payload = {
        "workspace_id": workspace_id,
        "user_id":      user_id,
        "exp":          int(time.time()) + STATE_LIFETIME_SECONDS,
        "nonce":        secrets.token_urlsafe(8),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return _b64url_encode(raw) + "." + _b64url_encode(sig)


def verify_oauth_state(state: str) -> Optional[Dict[str, Any]]:
    """
    Validate a state token returned by Slack. Returns the decoded
    payload dict on success, or None on any failure (bad format, bad
    signature, expired, missing secret). Never raises — callers branch
    on None.
    """
    if not state or "." not in state:
        return None
    secret = _state_secret()
    if not secret:
        return None
    try:
        payload_b64, sig_b64 = state.split(".", 1)
        raw = _b64url_decode(payload_b64)
        sig = _b64url_decode(sig_b64)
    except Exception:  # noqa: BLE001
        return None

    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None

    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    if not payload.get("workspace_id") or not payload.get("user_id"):
        return None
    return payload


# ---------------------------------------------------------------------- #
# Building the Connect-Slack URL
# ---------------------------------------------------------------------- #

def build_connect_url(*, workspace_id: str, user_id: str) -> str:
    """
    Build the full Slack OAuth v2 authorize URL the frontend should
    redirect the user to. Includes a signed state binding this attempt
    to the current workspace + user.
    """
    state = make_oauth_state(workspace_id, user_id)
    qs = urlencode({
        "client_id":    _client_id(),
        "scope":        ",".join(DEFAULT_SCOPES),
        "user_scope":   "",
        "redirect_uri": _redirect_uri(),
        "state":        state,
    })
    return f"https://slack.com/oauth/v2/authorize?{qs}"


# ---------------------------------------------------------------------- #
# OAuth code exchange
# ---------------------------------------------------------------------- #
# Slack's v2 oauth.access response shape:
#   {
#     "ok": true,
#     "access_token": "xoxb-...",         <-- the bot token
#     "scope": "channels:history,...",
#     "bot_user_id": "U...",
#     "team": {"id": "T...", "name": "..."},
#     ...
#   }
# Documented at https://api.slack.com/methods/oauth.v2.access.


def exchange_code(code: str) -> Optional[Dict[str, Any]]:
    """
    Exchange an OAuth code for a bot token via Slack's oauth.v2.access.
    Returns the parsed JSON on success, None on any failure.
    """
    try:
        resp = requests.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id":     _client_id(),
                "client_secret": _client_secret(),
                "code":          code,
                "redirect_uri":  _redirect_uri(),
            },
            timeout=15,
        )
    except requests.RequestException as e:
        logger.warning("slack_oauth_exchange_request_failed",
                       extra={"error": type(e).__name__})
        return None
    if not resp.ok:
        logger.warning("slack_oauth_exchange_http_error",
                       extra={"status": resp.status_code})
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        logger.warning("slack_oauth_exchange_not_ok",
                       extra={"error": (data or {}).get("error")})
        return None
    return data


def installation_from_oauth_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Project a Slack oauth.v2.access response into the row we store in
    public.slack_installations. Defensive: missing fields collapse to
    empty strings rather than throwing — the upsert can still proceed.
    """
    team = data.get("team") or {}
    return {
        "slack_team_id":   (team.get("id") or "").strip(),
        "slack_team_name": (team.get("name") or "").strip(),
        "bot_user_id":     (data.get("bot_user_id") or "").strip(),
        "bot_token":       (data.get("access_token") or "").strip(),
        "scopes":          (data.get("scope") or "").strip(),
    }


# ---------------------------------------------------------------------- #
# Listing channels from Slack (after Connect, so the picker can populate)
# ---------------------------------------------------------------------- #

def list_slack_channels(bot_token: str) -> List[Dict[str, Any]]:
    """
    Enumerate channels the bot can see via conversations.list. Returns
    a list of {slack_channel_id, name, is_archived} dicts ready to upsert
    into slack_channels. We paginate transparently — Slack caps each
    page at 1000.

    Failures return an empty list and log a warning. The caller decides
    whether to surface that to the user.
    """
    if not bot_token:
        return []
    client = WebClient(token=bot_token)
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    pages = 0
    try:
        while True:
            kwargs: Dict[str, Any] = {
                "exclude_archived": False,
                "limit":            1000,
                "types":            "public_channel,private_channel",
            }
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.conversations_list(**kwargs)
            for ch in resp.get("channels", []) or []:
                cid = (ch.get("id") or "").strip()
                if not cid:
                    continue
                out.append({
                    "slack_channel_id": cid,
                    "name":             (ch.get("name") or "").strip(),
                    "is_archived":      bool(ch.get("is_archived")),
                })
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
            pages += 1
            # Safety: Slack workspaces with >10k channels are exotic and
            # we'd rather return a partial list than spin forever.
            if not cursor or pages >= 20:
                break
    except SlackApiError as e:
        logger.warning("slack_conversations_list_failed",
                       extra={"error": str(e)})
        return out
    return out


# ---------------------------------------------------------------------- #
# Per-workspace ingestion runner
# ---------------------------------------------------------------------- #
# Re-uses the existing ingestion primitives (process_channel,
# upload_in_batches, IngestionState) but threads through an explicit
# bot_token + channel id list instead of reading from env. The state
# file path is shared with the prototype on purpose — moving state into
# Supabase is explicitly out of scope for Phase 3.


def run_workspace_ingest(
    *, workspace_id: str, bot_token: str, channel_ids: List[str],
    hydradb_sub_tenant_id: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Run a synchronous ingestion pass for one workspace's selected
    channels. Returns a small stats dict.

    Phase 4: when `hydradb_sub_tenant_id` is provided we route all
    HydraDB uploads to that sub-tenant so workspaces are isolated.
    When omitted (legacy CLI path / older tests) we fall back to the
    HYDRADB_SUB_TENANT_ID env default. The /api/slack/ingest route
    resolves the workspace's sub-tenant via ensure_workspace_sub_tenant
    before scheduling this runner.

    Synchronous on purpose: the caller wires this into a FastAPI
    BackgroundTask so the request returns immediately and the run
    proceeds in the worker. If anything raises, we log and return a
    failure dict — never propagate to the caller.
    """
    if not bot_token or not channel_ids:
        return {
            "channels_processed": 0,
            "files_prepared":     0,
            "successes":          0,
            "failures":           0,
            "skipped":            0,
        }

    # Lazy imports so a missing slack_sdk dep at startup doesn't tank
    # the whole module (and so the test suite can monkeypatch).
    from ingestion.ingest_slack import (
        process_channel, upload_in_batches, STATE_PATH,
    )
    from ingestion.ingestion_state import IngestionState
    from ingestion.slack_client import SlackClientWrapper
    from hydradb_client import HydraDBClient

    slack = SlackClientWrapper(token=bot_token)
    # Phase 4: route uploads to the workspace's HydraDB sub-tenant.
    # Falling back to the env default would silently leak this
    # workspace's documents into another tenant — surface that as a
    # log warning so an operator can see the misconfiguration.
    if hydradb_sub_tenant_id:
        hydra = HydraDBClient(sub_tenant_id=hydradb_sub_tenant_id)
    else:
        logger.warning(
            "workspace_ingest_no_sub_tenant",
            extra={"workspace_id": workspace_id},
        )
        hydra = HydraDBClient()
    state = IngestionState(STATE_PATH)

    total_files = 0
    total_success = 0
    total_failure = 0
    total_skipped = 0
    processed = 0

    for channel_id in channel_ids:
        try:
            result = process_channel(slack, channel_id, state, force=force)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "workspace_ingest_channel_error",
                extra={
                    "workspace_id": workspace_id,
                    "channel_id":   channel_id,
                    "error":        type(e).__name__,
                },
            )
            continue
        processed += 1
        total_files += len(result.get("files") or [])
        total_skipped += result.get("skipped_count", 0)

        files = result.get("files") or []
        if not files:
            newest = result.get("newest_ts_seen")
            if newest:
                state.set_last_synced_ts(result["channel_id"], newest)
                state.save()
            continue

        stats = upload_in_batches(hydra, files, state)
        total_success += stats.get("successes", 0)
        total_failure += stats.get("failures", 0)

        newest = result.get("newest_ts_seen")
        if newest and stats.get("failures", 0) == 0:
            state.set_last_synced_ts(result["channel_id"], newest)
            state.save()

    logger.info(
        "workspace_ingest_complete",
        extra={
            "workspace_id":       workspace_id,
            "channels_processed": processed,
            "files_prepared":     total_files,
            "successes":          total_success,
            "failures":           total_failure,
            "skipped":            total_skipped,
        },
    )
    return {
        "channels_processed": processed,
        "files_prepared":     total_files,
        "successes":          total_success,
        "failures":           total_failure,
        "skipped":            total_skipped,
    }