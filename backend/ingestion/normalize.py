"""
Normalize raw Slack messages into clean knowledge documents
ready for HydraDB ingestion.

Each returned document has the shape:
    {
        "content":  str,   # human-readable text we want indexed
        "metadata": dict,  # structured fields for filtering / retrieval
    }
"""

from typing import Any, Dict, List


# Slack `subtype` values that are operational noise we don't want to index.
# Regular user messages have no `subtype` (or it's missing entirely).
NOISE_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "pinned_item",
    "unpinned_item",
    "bot_add",
    "bot_remove",
    "channel_convert_to_private",
    "channel_convert_to_public",
}


def is_noise(message: Dict[str, Any]) -> bool:
    """Return True if this Slack message should be skipped."""
    if message.get("subtype") in NOISE_SUBTYPES:
        return True
    # Drop messages with no text content at all.
    if not (message.get("text") or "").strip():
        return True
    return False


def is_thread_parent(message: Dict[str, Any]) -> bool:
    """
    A message is a thread parent if it has at least one reply.

    In Slack, the parent has thread_ts == ts AND reply_count > 0.
    A reply has thread_ts set but thread_ts != ts.
    """
    reply_count = int(message.get("reply_count") or 0)
    if reply_count <= 0:
        return False
    thread_ts = message.get("thread_ts")
    ts = message.get("ts")
    # If thread_ts is missing we still treat reply_count > 0 as a parent signal.
    return thread_ts is None or thread_ts == ts


def is_thread_reply(message: Dict[str, Any]) -> bool:
    """True if this message is a reply that lives inside a thread."""
    thread_ts = message.get("thread_ts")
    ts = message.get("ts")
    return bool(thread_ts) and thread_ts != ts


# ---------------------------------------------------------------------- #
# Document builders
# ---------------------------------------------------------------------- #
def normalize_slack_message(
    message: Dict[str, Any],
    channel_id: str,
) -> Dict[str, Any]:
    """Turn one standalone Slack message into a knowledge document."""
    ts = message.get("ts")
    user_id = message.get("user") or message.get("bot_id") or "unknown"
    text = (message.get("text") or "").strip()

    content = f"[{ts}] {user_id}: {text}"

    metadata = {
        "source": "slack",
        "doc_type": "message",
        "channel_id": channel_id,
        "user_id": user_id,
        "ts": ts,
        "thread_ts": message.get("thread_ts"),
        "is_thread_parent": False,
        "reply_count": int(message.get("reply_count") or 0),
        # Slack does not return permalinks in conversations.history; obtaining
        # one requires a separate chat.getPermalink call. We leave it null for
        # the MVP (permalink is populated later by the ingestion pipeline).
        "permalink": message.get("permalink"),
    }

    return {"content": content, "metadata": metadata}


def normalize_slack_thread(
    parent_message: Dict[str, Any],
    replies: List[Dict[str, Any]],
    channel_id: str,
) -> Dict[str, Any]:
    """
    Combine a thread parent and its replies into a single readable document.

    `replies` is the raw list returned by conversations.replies, which
    includes the parent as its first element. We dedupe that here.
    """
    thread_ts = parent_message.get("ts")
    parent_user = parent_message.get("user") or parent_message.get("bot_id") or "unknown"
    parent_text = (parent_message.get("text") or "").strip()

    # Slack returns the parent as the first reply -> filter it out.
    real_replies = [m for m in replies if m.get("ts") != thread_ts and not is_noise(m)]

    lines: List[str] = []
    lines.append("# Slack Thread")
    lines.append(f"Channel: {channel_id}")
    lines.append(f"Thread: {thread_ts}")
    lines.append("")
    lines.append("Parent:")
    lines.append(f"[{thread_ts}] {parent_user}: {parent_text}")

    if real_replies:
        lines.append("")
        lines.append("Replies:")
        for reply in real_replies:
            r_ts = reply.get("ts")
            r_user = reply.get("user") or reply.get("bot_id") or "unknown"
            r_text = (reply.get("text") or "").strip()
            lines.append(f"[{r_ts}] {r_user}: {r_text}")

    content = "\n".join(lines)

    metadata = {
        "source": "slack",
        "doc_type": "thread",
        "channel_id": channel_id,
        "user_id": parent_user,
        "ts": thread_ts,
        "thread_ts": thread_ts,
        "is_thread_parent": True,
        "reply_count": len(real_replies),
        "permalink": parent_message.get("permalink"),
    }

    return {"content": content, "metadata": metadata}