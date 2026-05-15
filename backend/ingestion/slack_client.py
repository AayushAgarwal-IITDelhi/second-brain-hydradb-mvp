"""
Slack API client wrapper for the Second Brain MVP.

Wraps slack_sdk.WebClient with two simple methods:
    - fetch_channel_messages: pulls messages from a channel with pagination
    - fetch_thread_replies:   pulls replies for a single thread with pagination

Slack API errors are caught and printed so that one bad channel or thread
does not crash the whole ingestion run.
"""

import os
import time
from typing import Any, Dict, List, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


# Slack's max page size for conversations.history / conversations.replies is 200.
SLACK_MAX_PAGE_SIZE = 200


class SlackClientWrapper:
    def __init__(self, token: Optional[str] = None):
        token = token or os.getenv("SLACK_BOT_TOKEN")
        if not token:
            raise ValueError("SLACK_BOT_TOKEN is not set in the environment.")
        self.client = WebClient(token=token)

        # In-memory caches. They live for the lifetime of this wrapper
        # instance (i.e. one ingestion run), which is all we need to avoid
        # calling Slack repeatedly for the same user / message.
        #   user_id -> display name (or None when lookup failed)
        self._user_name_cache: Dict[str, Optional[str]] = {}
        #   (channel_id, ts) -> permalink (or None when lookup failed)
        self._permalink_cache: Dict[tuple, Optional[str]] = {}

    # ------------------------------------------------------------------ #
    # Channel-level messages
    # ------------------------------------------------------------------ #
    def fetch_channel_messages(
        self,
        channel_id: str,
        limit_per_channel: int = 500,
    ) -> List[Dict[str, Any]]:
        """
        Fetch up to `limit_per_channel` messages from a single Slack channel.

        Uses conversations.history with cursor pagination. Stops as soon as we
        hit limit_per_channel or run out of messages.
        Returns the raw Slack message dicts (no normalization here).
        """
        collected: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        page_size = min(SLACK_MAX_PAGE_SIZE, limit_per_channel)

        while True:
            try:
                response = self.client.conversations_history(
                    channel=channel_id,
                    limit=page_size,
                    cursor=cursor,
                )
            except SlackApiError as e:
                # Print the Slack-side error message and decide whether to retry.
                err = e.response.get("error", str(e)) if getattr(e, "response", None) else str(e)
                print(f"[slack_client] conversations_history failed for {channel_id}: {err}")

                if self._is_rate_limited(e):
                    self._sleep_for_retry(e)
                    continue
                break

            messages = response.get("messages", []) or []
            collected.extend(messages)

            if len(collected) >= limit_per_channel:
                collected = collected[:limit_per_channel]
                break

            cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break

        return collected

    # ------------------------------------------------------------------ #
    # Thread replies
    # ------------------------------------------------------------------ #
    def fetch_thread_replies(
        self,
        channel_id: str,
        thread_ts: str,
    ) -> List[Dict[str, Any]]:
        """
        Fetch all replies for a thread (Slack returns the parent as the first
        message, followed by replies). Uses conversations.replies with cursor
        pagination.
        """
        collected: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            try:
                response = self.client.conversations_replies(
                    channel=channel_id,
                    ts=thread_ts,
                    limit=SLACK_MAX_PAGE_SIZE,
                    cursor=cursor,
                )
            except SlackApiError as e:
                err = e.response.get("error", str(e)) if getattr(e, "response", None) else str(e)
                print(
                    f"[slack_client] conversations_replies failed for "
                    f"{channel_id}/{thread_ts}: {err}"
                )

                if self._is_rate_limited(e):
                    self._sleep_for_retry(e)
                    continue
                break

            messages = response.get("messages", []) or []
            collected.extend(messages)

            cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break

        return collected

    # ------------------------------------------------------------------ #
    # User name resolution (cached)
    # ------------------------------------------------------------------ #
    def resolve_user_name(self, user_id: Optional[str]) -> Optional[str]:
        """
        Look up a Slack user's readable name from their U... ID.

        Preference: real_name -> display_name -> name -> None.
        Cached in-memory so we hit users.info at most once per user_id
        per ingestion run. Returns None for falsy ids or on API errors.
        """
        if not user_id:
            return None

        if user_id in self._user_name_cache:
            return self._user_name_cache[user_id]

        name: Optional[str] = None
        try:
            response = self.client.users_info(user=user_id)
            user = response.get("user") or {}
            profile = user.get("profile") or {}
            # Order matters: real_name is usually the best human label.
            name = (
                profile.get("real_name")
                or profile.get("display_name")
                or user.get("real_name")
                or user.get("name")
                or None
            )
            if isinstance(name, str):
                name = name.strip() or None
        except SlackApiError as e:
            err = e.response.get("error", str(e)) if getattr(e, "response", None) else str(e)
            print(f"[slack_client] users_info failed for {user_id}: {err}")

        self._user_name_cache[user_id] = name
        return name

    # ------------------------------------------------------------------ #
    # Permalinks (cached)
    # ------------------------------------------------------------------ #
    def get_permalink(
        self,
        channel_id: str,
        message_ts: str,
    ) -> Optional[str]:
        """
        Return the Slack permalink for a (channel_id, message_ts) pair.

        Cached. Returns None on API error so ingestion never fails just
        because a permalink lookup failed.
        """
        if not channel_id or not message_ts:
            return None

        cache_key = (channel_id, message_ts)
        if cache_key in self._permalink_cache:
            return self._permalink_cache[cache_key]

        permalink: Optional[str] = None
        try:
            response = self.client.chat_getPermalink(
                channel=channel_id,
                message_ts=message_ts,
            )
            permalink = response.get("permalink") or None
        except SlackApiError as e:
            err = e.response.get("error", str(e)) if getattr(e, "response", None) else str(e)
            print(
                f"[slack_client] chat_getPermalink failed for "
                f"{channel_id}/{message_ts}: {err}"
            )

        self._permalink_cache[cache_key] = permalink
        return permalink

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_rate_limited(e: SlackApiError) -> bool:
        try:
            return e.response.status_code == 429
        except Exception:
            return False

    @staticmethod
    def _sleep_for_retry(e: SlackApiError) -> None:
        retry_after = 1
        try:
            retry_after = int(e.response.headers.get("Retry-After", 1))
        except Exception:
            pass
        print(f"[slack_client] Rate limited. Sleeping {retry_after}s and retrying.")
        time.sleep(retry_after)