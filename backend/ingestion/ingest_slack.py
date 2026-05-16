"""
CLI script: ingest Slack channels into HydraDB.

Run from the `backend/` directory:
    python -m ingestion.ingest_slack

Or directly:
    python backend/ingestion/ingest_slack.py

For each Slack channel listed in SLACK_CHANNEL_IDS:
    1. Pull messages via conversations.history (paginated).
    2. For any thread parent, pull replies via conversations.replies.
    3. Build one .md file per standalone message and per thread, with a
       source metadata block at the top.
    4. Skip anything already recorded in data/ingestion_state.json.
    5. Upload remaining .md files to HydraDB as multipart/form-data.
    6. Record each successful upload in ingestion_state.json so the next
       run doesn't re-upload the same documents.

Set FORCE_REINGEST=true in the environment to ignore existing state and
re-upload everything (state is still updated after a successful upload).
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow running this file directly: put backend/ on sys.path so the
# top-level hydradb_client import works either way.
_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _THIS_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402
from slack_sdk.errors import SlackApiError  # noqa: E402

from ingestion.slack_client import SlackClientWrapper  # noqa: E402
from ingestion.normalize import (  # noqa: E402
    is_noise,
    is_thread_parent,
    is_thread_reply,
)
from ingestion.ingestion_state import (  # noqa: E402
    IngestionState,
    stable_key_for_message,
    stable_key_for_thread,
)
from hydradb_client import HydraDBClient, summarize_upload_response  # noqa: E402


# Tuning knobs (overridable via env)
MESSAGES_PER_CHANNEL = int(os.getenv("SLACK_LIMIT_PER_CHANNEL", "500"))
UPLOAD_BATCH_SIZE = int(os.getenv("HYDRADB_BATCH_SIZE", "50"))

# Where the local dedupe-state JSON file lives.
STATE_PATH = _BACKEND_DIR / "data" / "ingestion_state.json"


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def parse_channel_ids() -> List[str]:
    raw = os.getenv("SLACK_CHANNEL_IDS", "")
    return [cid.strip() for cid in raw.split(",") if cid.strip()]


def force_reingest_enabled() -> bool:
    return os.getenv("FORCE_REINGEST", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def fetch_channel_name(slack: SlackClientWrapper, channel_id: str) -> str:
    """
    Look up the human-readable channel name once per channel.
    Falls back to the channel_id if the lookup fails (e.g. for DMs).
    """
    try:
        resp = slack.client.conversations_info(channel=channel_id)
        channel = resp.get("channel") or {}
        name = channel.get("name") or channel.get("name_normalized")
        if name:
            return name
    except SlackApiError as e:
        err = e.response.get("error", str(e)) if getattr(e, "response", None) else str(e)
        print(f"[ingest] conversations_info failed for {channel_id}: {err}")
    return channel_id


def _safe_filename_part(name: str) -> str:
    """Make a string safe to drop into a filename."""
    cleaned = []
    for ch in name:
        if ch.isalnum() or ch in ("-", "_"):
            cleaned.append(ch)
        else:
            cleaned.append("_")
    return "".join(cleaned) or "unknown"


def _ts_for_filename(ts: str) -> str:
    """Strip the fractional part of a Slack ts for use in a filename."""
    if not ts:
        return "unknown"
    return ts.split(".")[0]


def _make_snippet(text: str, limit: int = 200) -> str:
    """First ~limit characters of a message/thread parent's text, single-line."""
    if not text:
        return ""
    flat = " ".join(text.split())  # collapse newlines/whitespace
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "..."


# ---------------------------------------------------------------------- #
# Document builders -> {"filename", "content", "stable_key", ...metadata}
# ---------------------------------------------------------------------- #
def build_message_file(
    message: Dict[str, Any],
    channel_id: str,
    channel_name: str,
    slack: SlackClientWrapper,
) -> Dict[str, Any]:
    """Build a single .md file for a standalone Slack message."""
    ts = message.get("ts", "")
    user_id = message.get("user") or message.get("bot_id") or "unknown"
    text = (message.get("text") or "").strip()
    stable_key = stable_key_for_message(channel_id, ts)

    # Resolve readable user name (falls back to the U... id) and permalink.
    user_name = slack.resolve_user_name(message.get("user")) or user_id
    permalink = slack.get_permalink(channel_id, ts) if ts else None
    snippet = _make_snippet(text)

    header_lines = [
        "# Slack Message",
        f"Source Key: {stable_key}",
        f"Channel: {channel_name}",
        f"Channel ID: {channel_id}",
        f"Timestamp: {ts}",
        f"User: {user_name}",
    ]
    if permalink:
        header_lines.append(f"Permalink: {permalink}")

    content = "\n".join(header_lines + ["", text])

    filename = (
        f"slack_{_safe_filename_part(channel_name)}_"
        f"{_ts_for_filename(ts)}.md"
    )
    return {
        "filename": filename,
        "content": content,
        "stable_key": stable_key,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "ts": ts,
        "thread_ts": None,
        # Extra metadata for state.mark_uploaded:
        "user_name": user_name,
        "timestamp": ts,
        "snippet": snippet,
        "permalink": permalink,
        "document_type": "message",
    }


def build_thread_file(
    parent_message: Dict[str, Any],
    replies: List[Dict[str, Any]],
    channel_id: str,
    channel_name: str,
    slack: SlackClientWrapper,
) -> Dict[str, Any]:
    """Build a single .md file combining a thread parent and its replies."""
    thread_ts = parent_message.get("ts", "")
    parent_user_id = (
        parent_message.get("user") or parent_message.get("bot_id") or "unknown"
    )
    parent_text = (parent_message.get("text") or "").strip()
    stable_key = stable_key_for_thread(channel_id, thread_ts)

    parent_user_name = (
        slack.resolve_user_name(parent_message.get("user")) or parent_user_id
    )
    permalink = slack.get_permalink(channel_id, thread_ts) if thread_ts else None
    snippet = _make_snippet(parent_text)

    # Slack returns the parent as the first reply; drop it + any noise.
    real_replies = [
        m for m in replies if m.get("ts") != thread_ts and not is_noise(m)
    ]

    header_lines = [
        "# Slack Thread",
        f"Source Key: {stable_key}",
        f"Channel: {channel_name}",
        f"Channel ID: {channel_id}",
        f"Thread: {thread_ts}",
        f"Parent User: {parent_user_name}",
    ]
    if permalink:
        header_lines.append(f"Permalink: {permalink}")

    lines = header_lines + [
        "",
        "Parent:",
        f"[{thread_ts}] {parent_user_name}: {parent_text}",
    ]
    if real_replies:
        lines.append("")
        lines.append("Replies:")
        for reply in real_replies:
            r_ts = reply.get("ts", "")
            r_user_name = (
                slack.resolve_user_name(reply.get("user"))
                or reply.get("user")
                or reply.get("bot_id")
                or "unknown"
            )
            r_text = (reply.get("text") or "").strip()
            lines.append(f"[{r_ts}] {r_user_name}: {r_text}")

    content = "\n".join(lines)
    filename = (
        f"slack_{_safe_filename_part(channel_name)}_"
        f"{_ts_for_filename(thread_ts)}.md"
    )
    return {
        "filename": filename,
        "content": content,
        "stable_key": stable_key,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "ts": None,
        "thread_ts": thread_ts,
        # Extra metadata for state.mark_uploaded:
        "user_name": parent_user_name,
        "timestamp": thread_ts,
        "snippet": snippet,
        "permalink": permalink,
        "document_type": "thread",
    }


# ---------------------------------------------------------------------- #
# Channel processing
# ---------------------------------------------------------------------- #
def process_channel(
    slack: SlackClientWrapper,
    channel_id: str,
    state: IngestionState,
    force: bool,
) -> Dict[str, Any]:
    """
    Pull messages + threads for one channel, then split into:
      - files_to_upload: brand-new (or force-mode) docs
      - skipped_count:   docs already present in state (only when not force)

    Incremental sync:
      - If FORCE_REINGEST is OFF and we have a `last_synced_ts` for this
        channel, pass it as `oldest=` so Slack only returns newer messages.
      - We track the newest ts seen during this run. The caller advances
        the channel's watermark to it ONLY after the upload succeeds.
    """
    channel_name = fetch_channel_name(slack, channel_id)

    oldest = None if force else state.get_last_synced_ts(channel_id)
    if oldest:
        print(
            f"\n[ingest] Channel {channel_id} -> '{channel_name}'; "
            f"fetching messages newer than {oldest} ..."
        )
    else:
        print(
            f"\n[ingest] Channel {channel_id} -> '{channel_name}'; "
            f"fetching full history (no prior sync) ..."
        )

    raw_messages = slack.fetch_channel_messages(
        channel_id=channel_id,
        limit_per_channel=MESSAGES_PER_CHANNEL,
        oldest=oldest,
    )
    print(f"[ingest] Got {len(raw_messages)} raw messages from {channel_id}.")

    files_to_upload: List[Dict[str, Any]] = []
    skipped_count = 0
    threads_fetched = 0
    newest_ts_seen: Optional[str] = None  # advance watermark to this after upload

    for message in raw_messages:
        # Track the newest ts seen across ALL messages (including ones we
        # skip or filter as noise), so the watermark advances past them too
        # and the next run doesn't waste an API call to re-see them.
        msg_ts = message.get("ts") or ""
        if msg_ts and (newest_ts_seen is None or msg_ts > newest_ts_seen):
            newest_ts_seen = msg_ts

        if is_noise(message):
            continue

        if is_thread_parent(message):
            thread_ts = message.get("ts", "")
            stable_key = stable_key_for_thread(channel_id, thread_ts)
            if not force and state.has(stable_key):
                print(f"[ingest] skipping already uploaded: {stable_key}")
                skipped_count += 1
                continue

            print(f"[ingest]   -> fetching thread {thread_ts}")
            replies = slack.fetch_thread_replies(
                channel_id=channel_id,
                thread_ts=thread_ts,
            )
            threads_fetched += 1
            files_to_upload.append(
                build_thread_file(message, replies, channel_id, channel_name, slack)
            )
            continue

        # Skip thread replies surfaced in conversations.history; their
        # content lives in the thread document.
        if is_thread_reply(message):
            continue

        ts = message.get("ts", "")
        stable_key = stable_key_for_message(channel_id, ts)
        if not force and state.has(stable_key):
            print(f"[ingest] skipping already uploaded: {stable_key}")
            skipped_count += 1
            continue

        files_to_upload.append(
            build_message_file(message, channel_id, channel_name, slack)
        )

    return {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "raw_count": len(raw_messages),
        "thread_count": threads_fetched,
        "skipped_count": skipped_count,
        "files": files_to_upload,
        "newest_ts_seen": newest_ts_seen,
    }


# ---------------------------------------------------------------------- #
# Upload + state update
# ---------------------------------------------------------------------- #
def _result_is_success(result: Dict[str, Any]) -> bool:
    """Mirror summarize_upload_response's per-item rule."""
    status = (result.get("status") or "").lower()
    if status in ("failed", "error"):
        return False
    if result.get("error"):
        return False
    return True


def _record_successful_uploads(
    state: IngestionState,
    batch: List[Dict[str, Any]],
    response: Dict[str, Any],
) -> int:
    """
    Map HydraDB's per-file results back to the prepared files by filename
    and write successes into the state object. Returns how many docs we
    recorded.

    Falls back gracefully:
      - If `results` array exists, match by filename and use each result's
        own status / source_id.
      - If `results` is missing but the overall response says success
        (success_count > 0 or HTTP 2xx body with no per-item failures),
        record every file in the batch.
    """
    files_by_name = {f["filename"]: f for f in batch}
    recorded = 0

    results = response.get("results") if isinstance(response, dict) else None

    if isinstance(results, list) and results:
        for r in results:
            if not _result_is_success(r):
                continue
            filename = r.get("filename") or r.get("name")
            if not filename or filename not in files_by_name:
                continue
            f = files_by_name[filename]
            source_id = r.get("source_id") or r.get("id") or r.get("doc_id")
            state.mark_uploaded(
                stable_key=f["stable_key"],
                filename=f["filename"],
                channel_id=f["channel_id"],
                channel_name=f["channel_name"],
                ts=f.get("ts"),
                thread_ts=f.get("thread_ts"),
                source_id=source_id,
                user_name=f.get("user_name"),
                timestamp=f.get("timestamp"),
                snippet=f.get("snippet"),
                permalink=f.get("permalink"),
                document_type=f.get("document_type"),
            )
            recorded += 1
        return recorded

    # No per-file results -> fall back to summarize_upload_response's view.
    ok, _ = summarize_upload_response(response or {}, batch_size=len(batch))
    if ok > 0:
        # Best we can do without per-file feedback: record all files with
        # source_id=None. The next run will then skip them.
        for f in batch:
            state.mark_uploaded(
                stable_key=f["stable_key"],
                filename=f["filename"],
                channel_id=f["channel_id"],
                channel_name=f["channel_name"],
                ts=f.get("ts"),
                thread_ts=f.get("thread_ts"),
                source_id=None,
                user_name=f.get("user_name"),
                timestamp=f.get("timestamp"),
                snippet=f.get("snippet"),
                permalink=f.get("permalink"),
                document_type=f.get("document_type"),
            )
            recorded += 1

    return recorded


def upload_in_batches(
    hydra: HydraDBClient,
    files: List[Dict[str, Any]],
    state: IngestionState,
) -> Dict[str, int]:
    """
    Upload `files` to HydraDB in batches, tally success/failure, and
    persist state after each batch so an interrupted run doesn't lose
    progress.
    """
    successes = 0
    failures = 0

    for start in range(0, len(files), UPLOAD_BATCH_SIZE):
        batch = files[start:start + UPLOAD_BATCH_SIZE]
        print(
            f"\n[ingest] Uploading batch {start}-{start + len(batch)} "
            f"({len(batch)} files) ..."
        )

        # The HydraDB client expects {filename, content} dicts; our prepared
        # files carry extra metadata fields too — those are harmless extras.
        response = hydra.upload_knowledge(batch)
        ok, bad = summarize_upload_response(
            response if isinstance(response, dict) else {},
            batch_size=len(batch),
        )
        successes += ok
        failures += bad

        _record_successful_uploads(
            state,
            batch,
            response if isinstance(response, dict) else {},
        )
        # Save after every batch so a crash mid-run still keeps partial progress.
        state.save()

    return {"successes": successes, "failures": failures}


# ---------------------------------------------------------------------- #
# Main
# ---------------------------------------------------------------------- #
def main() -> None:
    load_dotenv()

    channel_ids = parse_channel_ids()
    if not channel_ids:
        print("[ingest] No SLACK_CHANNEL_IDS configured. Set it in .env and try again.")
        sys.exit(1)

    force = force_reingest_enabled()
    if force:
        print("[ingest] FORCE_REINGEST=true -> ignoring existing state for dedupe.")
        print("[ingest] FORCE_REINGEST=true -> ignoring last_synced_ts watermarks.")

    slack = SlackClientWrapper()
    hydra = HydraDBClient()
    state = IngestionState(STATE_PATH)
    print(
        f"[ingest] Loaded ingestion state from {STATE_PATH} "
        f"({len(state.entries)} entries, "
        f"{len(state.channels)} channel watermarks)."
    )

    total_raw_messages = 0
    total_threads = 0
    total_skipped = 0
    total_files_prepared = 0
    total_successes = 0
    total_failures = 0

    # We upload per-channel (not in one giant batch across channels) so we
    # can advance each channel's `last_synced_ts` ONLY after that channel's
    # upload actually succeeded. If the process is killed mid-run, channels
    # that completed have their watermark moved; channels that hadn't
    # finished keep their old watermark and re-fetch the same window next
    # time — safe to retry.
    for channel_id in channel_ids:
        try:
            result = process_channel(slack, channel_id, state, force=force)
        except Exception as e:  # noqa: BLE001 -- keep going on bad channels
            print(f"[ingest] Unexpected error processing channel {channel_id}: {e}")
            continue

        total_raw_messages += result["raw_count"]
        total_threads += result["thread_count"]
        total_skipped += result["skipped_count"]
        total_files_prepared += len(result["files"])

        if not result["files"]:
            # Nothing new to upload. But if Slack returned messages at all
            # (e.g. ones we'd already ingested or filtered as noise), we can
            # safely advance the watermark to the newest ts we saw — that
            # avoids re-fetching them next run.
            newest = result.get("newest_ts_seen")
            if newest:
                state.set_last_synced_ts(result["channel_id"], newest)
                state.save()
                print(
                    f"[ingest] Channel {result['channel_id']}: nothing new, "
                    f"advanced last_synced_ts to {newest}."
                )
            continue

        stats = upload_in_batches(hydra, result["files"], state)
        total_successes += stats["successes"]
        total_failures += stats["failures"]

        # Advance the watermark ONLY if every file in this channel uploaded
        # OK. If even one failed, we keep the old watermark so the next run
        # re-fetches the failed window and tries again. (Stable-key dedupe
        # makes the retry safe: anything that did upload won't be re-sent.)
        newest = result.get("newest_ts_seen")
        if newest and stats["failures"] == 0:
            state.set_last_synced_ts(result["channel_id"], newest)
            state.save()
            print(
                f"[ingest] Channel {result['channel_id']}: upload OK, "
                f"advanced last_synced_ts to {newest}."
            )
        elif stats["failures"] > 0:
            print(
                f"[ingest] Channel {result['channel_id']}: "
                f"{stats['failures']} failure(s); leaving last_synced_ts "
                f"unchanged so the next run retries."
            )

    print("\n[ingest] ============================================")
    print(f"[ingest] Channels processed:       {len(channel_ids)}")
    print(f"[ingest] Raw messages fetched:     {total_raw_messages}")
    print(f"[ingest] Threads fetched:          {total_threads}")
    print(f"[ingest] Files prepared:           {total_files_prepared}")
    print(f"[ingest] Skipped (already in state):{total_skipped}")
    print(f"[ingest] Upload successes:         {total_successes}")
    print(f"[ingest] Upload failures:          {total_failures}")
    print(f"[ingest] State entries now:        {len(state.entries)} "
          f"(saved to {STATE_PATH})")
    print("[ingest] ============================================")


if __name__ == "__main__":
    main()