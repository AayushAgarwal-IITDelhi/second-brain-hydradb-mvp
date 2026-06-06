"""
Unit tests for ingestion/slack_client.py — SlackClientWrapper.

All Slack API calls (WebClient) are mocked; no real Slack credentials needed.
"""

from unittest.mock import MagicMock, call, patch

import pytest
from slack_sdk.errors import SlackApiError

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_wrapper(token="test-token"):
    """Return a SlackClientWrapper with a fully mocked WebClient."""
    with patch("ingestion.slack_client.WebClient") as MockWC:
        from ingestion.slack_client import SlackClientWrapper

        wrapper = SlackClientWrapper(token=token)
        # Replace the real client with the mock instance
        wrapper.client = MockWC.return_value
        return wrapper, wrapper.client


def _slack_error(error_code: str, status_code: int = 400) -> SlackApiError:
    """Build a minimal SlackApiError with a .response mock."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    resp.get = lambda key, default=None: (error_code if key == "error" else default)
    exc = SlackApiError(message=error_code, response=resp)
    exc.response = resp
    return exc


def _history_page(messages, next_cursor=None):
    resp = MagicMock()
    meta = {"next_cursor": next_cursor} if next_cursor else {}
    resp.get = lambda k, d=None: (messages if k == "messages" else (meta if k == "response_metadata" else d))
    return resp


# ── fetch_channel_messages ─────────────────────────────────────────────────


class TestFetchChannelMessages:
    def test_single_page_no_cursor(self):
        wrapper, client = _make_wrapper()
        msgs = [{"ts": "1.0", "text": "hello"}]
        client.conversations_history.return_value = _history_page(msgs)

        result = wrapper.fetch_channel_messages("C123")

        assert result == msgs
        assert client.conversations_history.call_count == 1

    def test_pagination_two_pages(self):
        wrapper, client = _make_wrapper()
        page1 = [{"ts": "1.0", "text": "a"}]
        page2 = [{"ts": "2.0", "text": "b"}]

        client.conversations_history.side_effect = [
            _history_page(page1, next_cursor="cursor-abc"),
            _history_page(page2, next_cursor=None),
        ]

        result = wrapper.fetch_channel_messages("C123")

        assert len(result) == 2
        assert client.conversations_history.call_count == 2
        # second call must include the cursor
        _, kwargs = client.conversations_history.call_args_list[1]
        assert kwargs.get("cursor") == "cursor-abc"

    def test_limit_caps_results(self):
        wrapper, client = _make_wrapper()
        # Return 5 messages; limit is 3
        msgs = [{"ts": f"{i}.0", "text": f"m{i}"} for i in range(5)]
        client.conversations_history.return_value = _history_page(msgs)

        result = wrapper.fetch_channel_messages("C123", limit_per_channel=3)

        assert len(result) == 3

    def test_oldest_kwarg_forwarded(self):
        wrapper, client = _make_wrapper()
        client.conversations_history.return_value = _history_page([])

        wrapper.fetch_channel_messages("C123", oldest="100.0")

        _, kwargs = client.conversations_history.call_args
        assert kwargs.get("oldest") == "100.0" or ("oldest" in client.conversations_history.call_args[1])

    def test_rate_limit_retries(self):
        wrapper, client = _make_wrapper()
        msgs = [{"ts": "1.0", "text": "after-retry"}]

        rate_err = _slack_error("ratelimited", status_code=429)
        client.conversations_history.side_effect = [
            rate_err,
            _history_page(msgs),
        ]

        with patch("ingestion.slack_client.time.sleep"):
            result = wrapper.fetch_channel_messages("C123")

        assert result == msgs
        assert client.conversations_history.call_count == 2

    def test_rate_limit_cap_gives_up_after_max_retries(self):
        from ingestion.slack_client import MAX_RATE_LIMIT_RETRIES

        wrapper, client = _make_wrapper()
        rate_err = _slack_error("ratelimited", status_code=429)
        # Always rate-limited — should stop after MAX_RATE_LIMIT_RETRIES retries.
        client.conversations_history.side_effect = rate_err

        with patch("ingestion.slack_client.time.sleep"):
            result = wrapper.fetch_channel_messages("C123")

        assert result == []
        assert client.conversations_history.call_count == MAX_RATE_LIMIT_RETRIES + 1

    def test_non_rate_limit_error_breaks_loop(self):
        wrapper, client = _make_wrapper()
        client.conversations_history.side_effect = _slack_error("not_in_channel", 403)

        result = wrapper.fetch_channel_messages("C123")

        assert result == []
        assert client.conversations_history.call_count == 1

    def test_missing_messages_key_treated_as_empty(self):
        wrapper, client = _make_wrapper()
        resp = MagicMock()
        resp.get = lambda k, d=None: (None if k == "messages" else d)
        client.conversations_history.return_value = resp

        result = wrapper.fetch_channel_messages("C123")

        assert result == []


# ── fetch_thread_replies ────────────────────────────────────────────────────


class TestFetchThreadReplies:
    def _replies_page(self, messages, next_cursor=None):
        resp = MagicMock()
        meta = {"next_cursor": next_cursor} if next_cursor else {}
        resp.get = lambda k, d=None: (messages if k == "messages" else (meta if k == "response_metadata" else d))
        return resp

    def test_single_page(self):
        wrapper, client = _make_wrapper()
        msgs = [{"ts": "1.0"}, {"ts": "2.0"}]
        client.conversations_replies.return_value = self._replies_page(msgs)

        result = wrapper.fetch_thread_replies("C123", "1.0")

        assert result == msgs
        assert client.conversations_replies.call_count == 1

    def test_pagination(self):
        wrapper, client = _make_wrapper()
        page1 = [{"ts": "1.0"}]
        page2 = [{"ts": "2.0"}]
        client.conversations_replies.side_effect = [
            self._replies_page(page1, next_cursor="c2"),
            self._replies_page(page2),
        ]

        result = wrapper.fetch_thread_replies("C123", "1.0")

        assert len(result) == 2

    def test_rate_limit_retries(self):
        wrapper, client = _make_wrapper()
        rate_err = _slack_error("ratelimited", 429)
        msgs = [{"ts": "1.0"}]
        client.conversations_replies.side_effect = [
            rate_err,
            self._replies_page(msgs),
        ]

        with patch("ingestion.slack_client.time.sleep"):
            result = wrapper.fetch_thread_replies("C123", "1.0")

        assert result == msgs

    def test_rate_limit_cap_gives_up_after_max_retries(self):
        from ingestion.slack_client import MAX_RATE_LIMIT_RETRIES

        wrapper, client = _make_wrapper()
        rate_err = _slack_error("ratelimited", 429)
        client.conversations_replies.side_effect = rate_err

        with patch("ingestion.slack_client.time.sleep"):
            result = wrapper.fetch_thread_replies("C123", "1.0")

        assert result == []
        assert client.conversations_replies.call_count == MAX_RATE_LIMIT_RETRIES + 1

    def test_non_rate_limit_error_returns_empty(self):
        wrapper, client = _make_wrapper()
        client.conversations_replies.side_effect = _slack_error("channel_not_found")

        result = wrapper.fetch_thread_replies("C123", "1.0")

        assert result == []


# ── resolve_user_name ──────────────────────────────────────────────────────


class TestResolveUserName:
    def test_returns_real_name(self):
        wrapper, client = _make_wrapper()
        client.users_info.return_value = {"user": {"profile": {"real_name": "Alice Smith"}, "name": "alice"}}

        assert wrapper.resolve_user_name("U001") == "Alice Smith"

    def test_falls_back_to_display_name(self):
        wrapper, client = _make_wrapper()
        client.users_info.return_value = {"user": {"profile": {"real_name": "", "display_name": "alice_d"}}}

        assert wrapper.resolve_user_name("U001") == "alice_d"

    def test_cache_hit_skips_api(self):
        wrapper, client = _make_wrapper()
        client.users_info.return_value = {"user": {"profile": {"real_name": "Bob"}}}

        wrapper.resolve_user_name("U002")
        wrapper.resolve_user_name("U002")  # second call

        assert client.users_info.call_count == 1

    def test_none_user_id_returns_none(self):
        wrapper, _ = _make_wrapper()
        assert wrapper.resolve_user_name(None) is None

    def test_missing_profile_key(self):
        wrapper, client = _make_wrapper()
        # Response has no 'profile' key under 'user'
        client.users_info.return_value = {"user": {"name": "charlie"}}

        result = wrapper.resolve_user_name("U003")
        assert result == "charlie"

    def test_api_error_returns_none(self):
        wrapper, client = _make_wrapper()
        client.users_info.side_effect = _slack_error("user_not_found")

        assert wrapper.resolve_user_name("U999") is None


# ── get_permalink ──────────────────────────────────────────────────────────


class TestGetPermalink:
    def test_returns_permalink(self):
        wrapper, client = _make_wrapper()
        client.chat_getPermalink.return_value = MagicMock(
            get=lambda k, d=None: "https://slack.com/perma" if k == "permalink" else d
        )

        result = wrapper.get_permalink("C123", "1.0")
        assert result == "https://slack.com/perma"

    def test_cache_hit_skips_api(self):
        wrapper, client = _make_wrapper()
        client.chat_getPermalink.return_value = MagicMock(
            get=lambda k, d=None: "https://slack.com/p" if k == "permalink" else d
        )

        wrapper.get_permalink("C123", "1.0")
        wrapper.get_permalink("C123", "1.0")

        assert client.chat_getPermalink.call_count == 1

    def test_api_error_returns_none(self):
        wrapper, client = _make_wrapper()
        client.chat_getPermalink.side_effect = _slack_error("message_not_found")

        assert wrapper.get_permalink("C123", "bad_ts") is None

    def test_missing_channel_returns_none(self):
        wrapper, _ = _make_wrapper()
        assert wrapper.get_permalink("", "1.0") is None


# ── _is_rate_limited / _sleep_for_retry ────────────────────────────────────


class TestHelpers:
    def test_is_rate_limited_true_for_429(self):
        from ingestion.slack_client import SlackClientWrapper

        err = _slack_error("ratelimited", 429)
        assert SlackClientWrapper._is_rate_limited(err) is True

    def test_is_rate_limited_false_for_403(self):
        from ingestion.slack_client import SlackClientWrapper

        err = _slack_error("not_authed", 403)
        assert SlackClientWrapper._is_rate_limited(err) is False

    def test_sleep_for_retry_uses_header(self):
        from ingestion.slack_client import SlackClientWrapper

        err = _slack_error("ratelimited", 429)
        headers = MagicMock()
        headers.get = lambda k, d=1: 3 if k == "Retry-After" else d
        err.response.headers = headers

        with patch("ingestion.slack_client.time.sleep") as mock_sleep:
            SlackClientWrapper._sleep_for_retry(err)

        mock_sleep.assert_called_once_with(3)

    def test_sleep_for_retry_defaults_to_1_when_no_header(self):
        from ingestion.slack_client import SlackClientWrapper

        err = _slack_error("ratelimited", 429)
        headers = MagicMock()
        headers.get = lambda k, d=1: d  # always returns default
        err.response.headers = headers

        with patch("ingestion.slack_client.time.sleep") as mock_sleep:
            SlackClientWrapper._sleep_for_retry(err)

        mock_sleep.assert_called_once_with(1)

    def test_is_rate_limited_false_when_response_raises(self):
        """Defensive branch: if .response.status_code raises, returns False."""
        from ingestion.slack_client import SlackClientWrapper

        err = _slack_error("ratelimited", 429)
        err.response = None  # accessing .status_code raises AttributeError

        assert SlackClientWrapper._is_rate_limited(err) is False

    def test_sleep_for_retry_defaults_to_1_when_headers_raise(self):
        """Defensive branch: if Retry-After parsing raises, falls back to 1."""
        from ingestion.slack_client import SlackClientWrapper

        err = _slack_error("ratelimited", 429)
        err.response.headers = None  # .get(...) raises AttributeError

        with patch("ingestion.slack_client.time.sleep") as mock_sleep:
            SlackClientWrapper._sleep_for_retry(err)

        mock_sleep.assert_called_once_with(1)

    def test_init_raises_when_no_token(self):
        """ValueError is raised when no token is provided and env var is absent."""
        import os
        from ingestion.slack_client import SlackClientWrapper

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("SLACK_BOT_TOKEN", None)
            with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
                with patch("ingestion.slack_client.WebClient"):
                    SlackClientWrapper(token=None)
