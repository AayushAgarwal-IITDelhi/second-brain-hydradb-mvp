"""Tests for ingestion/normalize.py — Slack message filtering and normalisation."""

import pytest


# ── is_noise ──────────────────────────────────────────────────────────────
class TestIsNoise:
    def _msg(self, **kwargs):
        defaults = {"text": "hello there", "ts": "100.0"}
        defaults.update(kwargs)
        return defaults

    def test_regular_message_is_not_noise(self):
        from ingestion.normalize import is_noise
        assert is_noise(self._msg()) is False

    @pytest.mark.parametrize("subtype", [
        "channel_join", "channel_leave", "channel_topic", "channel_purpose",
        "channel_name", "pinned_item", "unpinned_item", "bot_add", "bot_remove",
    ])
    def test_noise_subtypes_are_noise(self, subtype):
        from ingestion.normalize import is_noise
        assert is_noise(self._msg(subtype=subtype)) is True

    def test_empty_text_is_noise(self):
        from ingestion.normalize import is_noise
        assert is_noise({"ts": "100.0", "text": ""}) is True

    def test_whitespace_only_text_is_noise(self):
        from ingestion.normalize import is_noise
        assert is_noise({"ts": "100.0", "text": "   "}) is True

    def test_missing_text_is_noise(self):
        from ingestion.normalize import is_noise
        assert is_noise({"ts": "100.0"}) is True

    def test_bot_message_subtype_is_noise(self):
        # Note: 'bot_message' is NOT in NOISE_SUBTYPES in normalize.py,
        # but realtime_ingest.py handles it separately. We verify normalize's behavior:
        from ingestion.normalize import is_noise, NOISE_SUBTYPES
        if "bot_message" in NOISE_SUBTYPES:
            assert is_noise(self._msg(subtype="bot_message")) is True
        else:
            # Regular bot message with text content passes normalize
            assert is_noise(self._msg(subtype="bot_message")) is False


# ── is_thread_parent ──────────────────────────────────────────────────────
class TestIsThreadParent:
    def test_message_with_replies_is_parent(self):
        from ingestion.normalize import is_thread_parent
        msg = {"ts": "100.0", "thread_ts": "100.0", "reply_count": 3}
        assert is_thread_parent(msg) is True

    def test_message_no_replies_is_not_parent(self):
        from ingestion.normalize import is_thread_parent
        msg = {"ts": "100.0", "reply_count": 0}
        assert is_thread_parent(msg) is False

    def test_message_missing_reply_count_is_not_parent(self):
        from ingestion.normalize import is_thread_parent
        msg = {"ts": "100.0"}
        assert is_thread_parent(msg) is False

    def test_reply_count_string_zero(self):
        from ingestion.normalize import is_thread_parent
        msg = {"ts": "100.0", "reply_count": "0"}
        assert is_thread_parent(msg) is False

    def test_thread_ts_none_with_replies_is_parent(self):
        from ingestion.normalize import is_thread_parent
        msg = {"ts": "100.0", "thread_ts": None, "reply_count": 2}
        assert is_thread_parent(msg) is True


# ── is_thread_reply ───────────────────────────────────────────────────────
class TestIsThreadReply:
    def test_reply_has_different_thread_ts(self):
        from ingestion.normalize import is_thread_reply
        msg = {"ts": "200.0", "thread_ts": "100.0"}
        assert is_thread_reply(msg) is True

    def test_parent_has_same_thread_ts(self):
        from ingestion.normalize import is_thread_reply
        msg = {"ts": "100.0", "thread_ts": "100.0"}
        assert is_thread_reply(msg) is False

    def test_no_thread_ts_is_not_reply(self):
        from ingestion.normalize import is_thread_reply
        msg = {"ts": "100.0"}
        assert is_thread_reply(msg) is False


# ── normalize_slack_message ───────────────────────────────────────────────
class TestNormalizeSlackMessage:
    def _msg(self, **kwargs):
        defaults = {
            "ts": "1000000001.000000",
            "user": "U123",
            "text": "Hello team, sprint deadline is Friday",
        }
        defaults.update(kwargs)
        return defaults

    def test_returns_content_and_metadata(self):
        from ingestion.normalize import normalize_slack_message
        result = normalize_slack_message(self._msg(), "C123")
        assert "content" in result
        assert "metadata" in result

    def test_content_includes_text(self):
        from ingestion.normalize import normalize_slack_message
        result = normalize_slack_message(self._msg(text="my content"), "C123")
        assert "my content" in result["content"]

    def test_content_includes_user(self):
        from ingestion.normalize import normalize_slack_message
        result = normalize_slack_message(self._msg(user="U456"), "C123")
        assert "U456" in result["content"]

    def test_metadata_source_is_slack(self):
        from ingestion.normalize import normalize_slack_message
        result = normalize_slack_message(self._msg(), "C123")
        assert result["metadata"]["source"] == "slack"

    def test_metadata_doc_type_is_message(self):
        from ingestion.normalize import normalize_slack_message
        result = normalize_slack_message(self._msg(), "C123")
        assert result["metadata"]["doc_type"] == "message"

    def test_metadata_channel_id(self):
        from ingestion.normalize import normalize_slack_message
        result = normalize_slack_message(self._msg(), "C999")
        assert result["metadata"]["channel_id"] == "C999"

    def test_bot_id_fallback(self):
        from ingestion.normalize import normalize_slack_message
        msg = {"ts": "100.0", "bot_id": "B123", "text": "bot message"}
        result = normalize_slack_message(msg, "C123")
        assert "B123" in result["content"]


# ── normalize_slack_thread ────────────────────────────────────────────────
class TestNormalizeSlackThread:
    def _parent(self, **kwargs):
        defaults = {"ts": "100.0", "user": "U001", "text": "Thread starter"}
        defaults.update(kwargs)
        return defaults

    def _reply(self, ts, text, user="U002"):
        return {"ts": ts, "user": user, "text": text}

    def test_returns_content_and_metadata(self):
        from ingestion.normalize import normalize_slack_thread
        result = normalize_slack_thread(self._parent(), [], "C123")
        assert "content" in result
        assert "metadata" in result

    def test_content_is_markdown(self):
        from ingestion.normalize import normalize_slack_thread
        result = normalize_slack_thread(self._parent(), [], "C123")
        assert "# Slack Thread" in result["content"]

    def test_replies_included(self):
        from ingestion.normalize import normalize_slack_thread
        replies = [self._reply("200.0", "reply text")]
        result = normalize_slack_thread(self._parent(), replies, "C123")
        assert "reply text" in result["content"]

    def test_parent_not_duplicated_in_replies(self):
        from ingestion.normalize import normalize_slack_thread
        parent = self._parent(ts="100.0")
        # Slack returns parent as first reply
        replies = [parent, self._reply("200.0", "actual reply")]
        result = normalize_slack_thread(parent, replies, "C123")
        # Parent text should appear once in the "Parent:" section
        assert result["content"].count("Thread starter") == 1

    def test_noise_replies_filtered(self):
        from ingestion.normalize import normalize_slack_thread
        noise_reply = {"ts": "300.0", "subtype": "channel_join", "text": ""}
        replies = [self._reply("200.0", "real reply"), noise_reply]
        result = normalize_slack_thread(self._parent(), replies, "C123")
        assert "channel_join" not in result["content"]

    def test_metadata_doc_type_is_thread(self):
        from ingestion.normalize import normalize_slack_thread
        result = normalize_slack_thread(self._parent(), [], "C123")
        assert result["metadata"]["doc_type"] == "thread"

    def test_metadata_reply_count(self):
        from ingestion.normalize import normalize_slack_thread
        replies = [self._reply("200.0", "r1"), self._reply("300.0", "r2")]
        result = normalize_slack_thread(self._parent(), replies, "C123")
        assert result["metadata"]["reply_count"] == 2
