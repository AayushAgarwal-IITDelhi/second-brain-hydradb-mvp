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