"""
Realtime Slack event -> HydraDB pipeline.

Called from main.py's /slack/events handler as a FastAPI BackgroundTask
after we've already returned 200 to Slack. Re-uses the existing builders
from ingestion.ingest_slack so the document format and stable-key dedupe
are identical to the polling path.

Concurrency notes:
- Slack delivers events one at a time per channel but globally many can
  arrive at once. FastAPI runs BackgroundTasks sequentially per request,
  but multiple requests can fire BackgroundTasks in parallel.
- The IngestionState file is rewritten atomically (write-temp-then-rename)
  but read-modify-write isn't safe without a lock. We use a module-level
  threading.Lock to serialize the realtime path. The polling CLI runs in
  its own process and is not affected.
"""

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

from hydradb_client import HydraDBClient, summarize_upload_response
from ingestion.ingest_slack import (
    _record_successful_uploads,
    build_message_file,
    build_thread_file,
    fetch_channel_name,
)
from ingestion.ingestion_state import (
    IngestionState,
    stable_key_for_message,
)
from ingestion.normalize import is_noise
from ingestion.slack_client import SlackClientWrapper
from logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------- #
# Config
# ---------------------------------------------------------------------- #
def _realtime_enabled() -> bool:
    return os.getenv("REALTIME_INGEST_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def _allowed_channel_ids() -> Set[str]:
    """Channels whose events we'll act on. Empty = ingest all channels."""
    raw = os.getenv("SLACK_CHANNEL_IDS", "")
    return {cid.strip() for cid in raw.split(",") if cid.strip()}


# Path to the same state file the polling CLI uses.
_BACKEND_DIR = Path(__file__).resolve().parent
STATE_PATH = _BACKEND_DIR / "data" / "ingestion_state.json"


# ---------------------------------------------------------------------- #
# Idempotency: dedupe Slack event retries (~3 retries within 1 hour)
# ---------------------------------------------------------------------- #
_SEEN_EVENT_TTL = 60 * 60  # 1 hour matches Slack's retry window
_SEEN_EVENT_MAX = 5000  # cap memory
_seen_event_ids: Dict[str, float] = {}
_seen_lock = threading.Lock()


def _event_already_seen(event_id: str) -> bool:
    """
    Return True if we've already processed this Slack event_id.
    Marks it as seen as a side effect (best-effort, in-memory).
    """
    if not event_id:
        return False
    now = time.monotonic()
    with _seen_lock:
        # Drop stale entries lazily — cheaper than a background sweeper.
        if len(_seen_event_ids) > _SEEN_EVENT_MAX:
            cutoff = now - _SEEN_EVENT_TTL
            for k, t in list(_seen_event_ids.items()):
                if t < cutoff:
                    _seen_event_ids.pop(k, None)
        if event_id in _seen_event_ids:
            if now - _seen_event_ids[event_id] < _SEEN_EVENT_TTL:
                return True
        _seen_event_ids[event_id] = now
        return False


# ---------------------------------------------------------------------- #
# Bot identity (so we can ignore our own bot's messages)
# ---------------------------------------------------------------------- #
_bot_user_id: Optional[str] = None
_bot_user_id_lock = threading.Lock()


def _resolve_bot_user_id(slack: SlackClientWrapper) -> Optional[str]:
    """
    Cache the bot's own user_id from auth.test. Called once per process.
    If the call fails we return None and fall back to the bot_id-based
    filter alone (still catches the common case).
    """
    global _bot_user_id
    with _bot_user_id_lock:
        if _bot_user_id is not None:
            return _bot_user_id or None
        try:
            resp = slack.client.auth_test()
            uid = resp.get("user_id") or ""
            _bot_user_id = uid if isinstance(uid, str) else ""
        except Exception as e:  # noqa: BLE001
            logger.warning('realtime_auth_test_failed', extra={'error': type(e).__name__})
            _bot_user_id = ""
    return _bot_user_id or None


# ---------------------------------------------------------------------- #
# Single shared lock around state read-modify-write for the realtime path
# ---------------------------------------------------------------------- #
_state_lock = threading.Lock()


# ---------------------------------------------------------------------- #
# Public entry point: handle one Slack event payload
# ---------------------------------------------------------------------- #
def process_slack_event(event: Dict[str, Any]) -> None:
    """
    Handle one inner Slack event (`payload["event"]`). Safe to call from
    a BackgroundTask. Catches everything so a single bad event never
    crashes the webhook worker.
    """
    if not _realtime_enabled():
        logger.debug('realtime_disabled')
        return

    try:
        _process_slack_event_inner(event)
    except Exception as e:  # noqa: BLE001
        logger.error('realtime_event_handler_failed', extra={'error': type(e).__name__})


def _process_slack_event_inner(event: Dict[str, Any]) -> None:
    event_type = event.get("type")
    if event_type != "message":
        logger.debug('realtime_event_ignored', extra={'event_type': event_type})
        return

    subtype = event.get("subtype")
    if subtype == "message_changed" or subtype == "message_deleted":
        logger.debug('realtime_event_ignored', extra={'subtype': subtype})
        return
    if subtype == "bot_message":
        logger.debug('realtime_event_ignored', extra={'subtype': 'bot_message'})
        return
    if is_noise(event):
        logger.debug('realtime_event_ignored', extra={'reason': 'noise', 'subtype': subtype})
        return

    channel_id = event.get("channel")
    if not channel_id:
        logger.debug('realtime_event_no_channel')
        return

    allowed = _allowed_channel_ids()
    if allowed and channel_id not in allowed:
        logger.debug('realtime_channel_not_allowed', extra={'channel_id': channel_id})
        return

    # ----- Build clients -----
    slack = SlackClientWrapper()
    hydra = HydraDBClient()

    # ----- Bot-loop guard -----
    bot_user_id = _resolve_bot_user_id(slack)
    if event.get("bot_id"):
        logger.debug('realtime_event_ignored', extra={'reason': 'bot_id_set'})
        return
    if bot_user_id and event.get("user") == bot_user_id:
        logger.debug('realtime_event_ignored', extra={'reason': 'own_bot_user'})
        return
    if not (event.get("text") or "").strip():
        logger.debug('realtime_event_ignored', extra={'reason': 'empty_text'})
        return

    channel_name = fetch_channel_name(slack, channel_id)

    # ----- Distinguish standalone vs thread -----
    # A message belongs to a thread if `thread_ts` is set AND differs
    # from its own `ts`. A thread parent appears as a normal message
    # event the first time it's posted (no thread_ts). When someone
    # replies, the reply has thread_ts != ts — we then re-upload the
    # whole thread doc (parent + all replies) so the stored markdown
    # always represents the current thread state.
    ts = event.get("ts") or ""
    thread_ts = event.get("thread_ts")
    is_reply = bool(thread_ts) and thread_ts != ts

    if is_reply:
        _ingest_thread(slack, hydra, channel_id, channel_name, thread_ts, event)
    else:
        _ingest_standalone(slack, hydra, channel_id, channel_name, event)


# ---------------------------------------------------------------------- #
# Ingestion paths
# ---------------------------------------------------------------------- #
def _ingest_standalone(
    slack: SlackClientWrapper,
    hydra: HydraDBClient,
    channel_id: str,
    channel_name: str,
    message: Dict[str, Any],
) -> None:
    """Build + upload a single standalone-message doc."""
    ts = message.get("ts", "")
    stable_key = stable_key_for_message(channel_id, ts)

    with _state_lock:
        state = IngestionState(STATE_PATH)
        if state.has(stable_key):
            logger.debug('realtime_already_ingested', extra={'stable_key': stable_key})
            return

    prepared = build_message_file(message, channel_id, channel_name, slack)
    logger.info('realtime_uploading', extra={'stable_key': stable_key, 'doc_type': 'message'})

    response = hydra.upload_knowledge([prepared])
    ok, _bad = summarize_upload_response(
        response if isinstance(response, dict) else {},
        batch_size=1,
    )
    if ok < 1:
        logger.warning('realtime_upload_failed', extra={'stable_key': stable_key})
        return

    with _state_lock:
        # IngestionState.locked() acquires an OS-level advisory lock, loads
        # fresh state from disk (cross-process safe), applies mutations, then
        # saves and releases automatically on exit.
        with IngestionState.locked(STATE_PATH) as state:
            _record_successful_uploads(state, [prepared], response or {})
            # Advance the per-channel watermark so the next polling pass
            # doesn't re-fetch this message just to skip it.
            state.set_last_synced_ts(channel_id, ts)
            state.touch_last_ingested()


def _ingest_thread(
    slack: SlackClientWrapper,
    hydra: HydraDBClient,
    channel_id: str,
    channel_name: str,
    thread_ts: str,
    triggering_event: Dict[str, Any],
) -> None:
    """
    Fetch the full thread (parent + all replies) and re-upload the
    consolidated thread doc. Safe under stable-key dedupe: the doc's
    stable_key is derived from (channel_id, thread_ts), so re-uploading
    overwrites or updates the same logical document.
    """
    # Pull the full thread from Slack so the doc reflects current state.
    replies = slack.fetch_thread_replies(
        channel_id=channel_id,
        thread_ts=thread_ts,
    )
    if not replies:
        logger.debug('realtime_thread_no_replies', extra={'thread_ts': thread_ts})
        return

    parent = replies[0]
    if is_noise(parent):
        logger.debug('realtime_thread_parent_noise')
        return

    prepared = build_thread_file(parent, replies, channel_id, channel_name, slack)
    stable_key = prepared["stable_key"]
    logger.info(
        'realtime_uploading',
        extra={
            'stable_key': stable_key,
            'doc_type': 'thread',
            'reply_count': len(replies),
        },
    )

    response = hydra.upload_knowledge([prepared])
    ok, _bad = summarize_upload_response(
        response if isinstance(response, dict) else {},
        batch_size=1,
    )
    if ok < 1:
        logger.warning('realtime_upload_failed', extra={'stable_key': stable_key})
        return

    with _state_lock:
        with IngestionState.locked(STATE_PATH) as state:
            # _record_successful_uploads is overwrite-by-stable-key inside
            # state.entries, so re-uploads correctly refresh the snippet /
            # uploaded_at / source_id without creating duplicates.
            _record_successful_uploads(state, [prepared], response or {})
            # Advance per-channel watermark to the triggering event's ts so
            # the next polling pass doesn't refetch this reply window.
            trigger_ts = triggering_event.get("ts")
            if trigger_ts:
                state.set_last_synced_ts(channel_id, trigger_ts)
            state.touch_last_ingested()


# ---------------------------------------------------------------------- #
# Status snapshot for the admin endpoint
# ---------------------------------------------------------------------- #
def admin_status_snapshot(scheduler_enabled: bool) -> Dict[str, Any]:
    """
    Light read-only snapshot of ingestion-related state.
    Called from /api/admin/status; cheap (single file read).
    """
    state = IngestionState(STATE_PATH)
    return {
        "realtime_ingest_enabled": _realtime_enabled(),
        "scheduler_enabled": scheduler_enabled,
        "last_ingested_at": state.get_last_ingested_at(),
        "total_docs": state.total_docs(),
        "channels_tracked": sum(1 for k in state.channels.keys() if k != "_meta"),
    }
