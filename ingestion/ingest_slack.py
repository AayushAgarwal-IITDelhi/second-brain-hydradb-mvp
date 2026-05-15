"""
CLI script: ingest Slack channels into HydraDB.

Run from project root:
    python -m ingestion.ingest_slack
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _THIS_DIR.parent

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402
from slack_sdk.errors import SlackApiError  # noqa: E402

from ingestion.slack_client import SlackClientWrapper  # noqa: E402
from ingestion.normalize import is_noise, is_thread_parent, is_thread_reply  # noqa: E402
from hydradb_client import HydraDBClient, summarize_upload_response  # noqa: E402


MESSAGES_PER_CHANNEL = int(os.getenv("SLACK_LIMIT_PER_CHANNEL", "500"))
UPLOAD_BATCH_SIZE = int(os.getenv("HYDRADB_BATCH_SIZE", "50"))


def parse_channel_ids() -> List[str]:
    raw = os.getenv("SLACK_CHANNEL_IDS", "")
    return [cid.strip() for cid in raw.split(",") if cid.strip()]


def fetch_channel_name(slack: SlackClientWrapper, channel_id: str) -> str:
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
    cleaned = []

    for ch in name:
        if ch.isalnum() or ch in ("-", "_"):
            cleaned.append(ch)
        else:
            cleaned.append("_")

    return "".join(cleaned) or "unknown"


def _ts_for_filename(ts: str) -> str:
    if not ts:
        return "unknown"

    return ts.split(".")[0]


def build_message_file(
    message: Dict[str, Any],
    channel_id: str,
    channel_name: str,
) -> Dict[str, str]:
    ts = message.get("ts", "")
    user = message.get("user") or message.get("bot_id") or "unknown"
    text = (message.get("text") or "").strip()

    content = "\n".join(
        [
            "# Slack Message",
            f"Channel: {channel_name}",
            f"Channel ID: {channel_id}",
            f"Timestamp: {ts}",
            f"User: {user}",
            "",
            text,
        ]
    )

    filename = (
        f"slack_{_safe_filename_part(channel_name)}_"
        f"{_ts_for_filename(ts)}.md"
    )

    return {
        "filename": filename,
        "content": content,
    }


def build_thread_file(
    parent_message: Dict[str, Any],
    replies: List[Dict[str, Any]],
    channel_id: str,
    channel_name: str,
) -> Dict[str, str]:
    thread_ts = parent_message.get("ts", "")
    parent_user = parent_message.get("user") or parent_message.get("bot_id") or "unknown"
    parent_text = (parent_message.get("text") or "").strip()

    real_replies = [
        m for m in replies
        if m.get("ts") != thread_ts and not is_noise(m)
    ]

    lines = [
        "# Slack Thread",
        f"Channel: {channel_name}",
        f"Channel ID: {channel_id}",
        f"Thread: {thread_ts}",
        "",
        "Parent:",
        f"[{thread_ts}] {parent_user}: {parent_text}",
    ]

    if real_replies:
        lines.append("")
        lines.append("Replies:")

        for reply in real_replies:
            r_ts = reply.get("ts", "")
            r_user = reply.get("user") or reply.get("bot_id") or "unknown"
            r_text = (reply.get("text") or "").strip()
            lines.append(f"[{r_ts}] {r_user}: {r_text}")

    content = "\n".join(lines)

    filename = (
        f"slack_{_safe_filename_part(channel_name)}_"
        f"{_ts_for_filename(thread_ts)}.md"
    )

    return {
        "filename": filename,
        "content": content,
    }


def process_channel(
    slack: SlackClientWrapper,
    channel_id: str,
) -> Dict[str, Any]:
    channel_name = fetch_channel_name(slack, channel_id)

    print(
        f"\n[ingest] Channel {channel_id} -> "
        f"'{channel_name}'; fetching messages ..."
    )

    raw_messages = slack.fetch_channel_messages(
        channel_id=channel_id,
        limit_per_channel=MESSAGES_PER_CHANNEL,
    )

    print(f"[ingest] Got {len(raw_messages)} raw messages from {channel_id}.")

    files: List[Dict[str, str]] = []
    threads_fetched = 0

    for message in raw_messages:
        if is_noise(message):
            continue

        if is_thread_parent(message):
            thread_ts = message.get("ts")
            print(f"[ingest]   -> fetching thread {thread_ts}")

            replies = slack.fetch_thread_replies(
                channel_id=channel_id,
                thread_ts=thread_ts,
            )

            threads_fetched += 1

            files.append(
                build_thread_file(
                    parent_message=message,
                    replies=replies,
                    channel_id=channel_id,
                    channel_name=channel_name,
                )
            )

            continue

        if is_thread_reply(message):
            continue

        files.append(
            build_message_file(
                message=message,
                channel_id=channel_id,
                channel_name=channel_name,
            )
        )

    return {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "raw_count": len(raw_messages),
        "thread_count": threads_fetched,
        "files": files,
    }


def upload_in_batches(
    hydra: HydraDBClient,
    files: List[Dict[str, str]],
) -> Dict[str, int]:
    successes = 0
    failures = 0

    for start in range(0, len(files), UPLOAD_BATCH_SIZE):
        batch = files[start:start + UPLOAD_BATCH_SIZE]

        print(
            f"\n[ingest] Uploading batch {start}-{start + len(batch)} "
            f"({len(batch)} files) ..."
        )

        response = hydra.upload_knowledge(batch)

        ok, bad = summarize_upload_response(
            response if isinstance(response, dict) else {},
            batch_size=len(batch),
        )

        successes += ok
        failures += bad

    return {
        "successes": successes,
        "failures": failures,
    }


def main() -> None:
    load_dotenv()

    channel_ids = parse_channel_ids()

    if not channel_ids:
        print("[ingest] No SLACK_CHANNEL_IDS configured. Set it in .env and try again.")
        sys.exit(1)

    slack = SlackClientWrapper()
    hydra = HydraDBClient()

    total_raw_messages = 0
    total_threads = 0
    all_files: List[Dict[str, str]] = []

    for channel_id in channel_ids:
        try:
            result = process_channel(slack, channel_id)
        except Exception as e:
            print(f"[ingest] Unexpected error processing channel {channel_id}: {e}")
            continue

        total_raw_messages += result["raw_count"]
        total_threads += result["thread_count"]
        all_files.extend(result["files"])

    print("\n[ingest] ============================================")
    print(f"[ingest] Channels processed:    {len(channel_ids)}")
    print(f"[ingest] Raw messages fetched:  {total_raw_messages}")
    print(f"[ingest] Threads fetched:       {total_threads}")
    print(f"[ingest] Knowledge files ready: {len(all_files)}")
    print("[ingest] ============================================")

    if not all_files:
        print("[ingest] Nothing to upload. Exiting.")
        return

    stats = upload_in_batches(hydra, all_files)

    print("\n[ingest] ============================================")
    print(f"[ingest] Upload successes: {stats['successes']}")
    print(f"[ingest] Upload failures:  {stats['failures']}")
    print("[ingest] ============================================")


if __name__ == "__main__":
    main()