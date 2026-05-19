"""Tests for ingestion/ingest_slack.py — CLI ingestion helpers."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────
def _msg(ts, text, user="U001", reply_count=0, thread_ts=None, subtype=None):
    m = {"ts": ts, "text": text, "user": user, "reply_count": reply_count}
    if thread_ts:
        m["thread_ts"] = thread_ts
    if subtype:
        m["subtype"] = subtype
    return m


def _mock_slack():
    slack = MagicMock()
    slack.client.conversations_info.return_value = {"channel": {"name": "general"}}
    slack.resolve_user_name.return_value = "Alice"
    slack.get_permalink.return_value = "https://slack.com/perma"
    slack.fetch_channel_messages.return_value = []
    slack.fetch_thread_replies.return_value = []
    return slack


# ── parse_channel_ids ────────────────────────────────────────────────────
class TestParseChannelIds:
    def test_parses_comma_separated(self):
        from ingestion.ingest_slack import parse_channel_ids
        with patch.dict(os.environ, {"SLACK_CHANNEL_IDS": "C1,C2,C3"}):
            assert parse_channel_ids() == ["C1", "C2", "C3"]

    def test_strips_whitespace(self):
        from ingestion.ingest_slack import parse_channel_ids
        with patch.dict(os.environ, {"SLACK_CHANNEL_IDS": " C1 , C2 "}):
            assert parse_channel_ids() == ["C1", "C2"]

    def test_empty_returns_empty_list(self):
        from ingestion.ingest_slack import parse_channel_ids
        with patch.dict(os.environ, {"SLACK_CHANNEL_IDS": ""}):
            assert parse_channel_ids() == []


# ── force_reingest_enabled ────────────────────────────────────────────────
class TestForceReingestEnabled:
    @pytest.mark.parametrize("val", ["true", "1", "yes", "on"])
    def test_truthy_values(self, val):
        from ingestion.ingest_slack import force_reingest_enabled
        with patch.dict(os.environ, {"FORCE_REINGEST": val}):
            assert force_reingest_enabled() is True

    @pytest.mark.parametrize("val", ["false", "0", "no", "off", ""])
    def test_falsy_values(self, val):
        from ingestion.ingest_slack import force_reingest_enabled
        with patch.dict(os.environ, {"FORCE_REINGEST": val}):
            assert force_reingest_enabled() is False


# ── _make_snippet ─────────────────────────────────────────────────────────
class TestMakeSnippet:
    def test_short_text_unchanged(self):
        from ingestion.ingest_slack import _make_snippet
        assert _make_snippet("hello") == "hello"

    def test_long_text_truncated_with_ellipsis(self):
        from ingestion.ingest_slack import _make_snippet
        text = "a" * 300
        result = _make_snippet(text)
        assert result.endswith("...")
        assert len(result) <= 203

    def test_empty_text_returns_empty(self):
        from ingestion.ingest_slack import _make_snippet
        assert _make_snippet("") == ""

    def test_newlines_collapsed(self):
        from ingestion.ingest_slack import _make_snippet
        result = _make_snippet("line1\nline2\nline3")
        assert "\n" not in result

    def test_custom_limit(self):
        from ingestion.ingest_slack import _make_snippet
        result = _make_snippet("x" * 100, limit=50)
        assert result.endswith("...")


# ── _safe_filename_part ───────────────────────────────────────────────────
class TestSafeFilenamePart:
    def test_alphanumeric_unchanged(self):
        from ingestion.ingest_slack import _safe_filename_part
        assert _safe_filename_part("hello123") == "hello123"

    def test_spaces_replaced(self):
        from ingestion.ingest_slack import _safe_filename_part
        result = _safe_filename_part("all second brain")
        assert " " not in result

    def test_hyphens_kept(self):
        from ingestion.ingest_slack import _safe_filename_part
        assert _safe_filename_part("all-second-brain") == "all-second-brain"

    def test_slashes_replaced(self):
        from ingestion.ingest_slack import _safe_filename_part
        result = _safe_filename_part("a/b")
        assert "/" not in result

    def test_empty_string_returns_unknown(self):
        from ingestion.ingest_slack import _safe_filename_part
        assert _safe_filename_part("") == "unknown"


# ── _ts_for_filename ──────────────────────────────────────────────────────
class TestTsForFilename:
    def test_strips_fractional_part(self):
        from ingestion.ingest_slack import _ts_for_filename
        assert _ts_for_filename("1778775842.876209") == "1778775842"

    def test_empty_returns_unknown(self):
        from ingestion.ingest_slack import _ts_for_filename
        assert _ts_for_filename("") == "unknown"

    def test_no_dot_returned_unchanged(self):
        from ingestion.ingest_slack import _ts_for_filename
        assert _ts_for_filename("1778775842") == "1778775842"


# ── build_message_file ────────────────────────────────────────────────────
class TestBuildMessageFile:
    def _build(self, **kwargs):
        from ingestion.ingest_slack import build_message_file
        msg = {
            "ts": "1000000001.000000",
            "user": "U001",
            "text": "hello team",
        }
        msg.update(kwargs)
        slack = _mock_slack()
        return build_message_file(msg, "C123", "general", slack)

    def test_returns_required_keys(self):
        result = self._build()
        assert "filename" in result
        assert "content" in result
        assert "stable_key" in result

    def test_stable_key_format(self):
        result = self._build()
        assert result["stable_key"].startswith("slack:C123:")

    def test_filename_extension(self):
        result = self._build()
        assert result["filename"].endswith(".md")

    def test_content_includes_text(self):
        result = self._build(text="important message")
        assert "important message" in result["content"]

    def test_content_has_header(self):
        result = self._build()
        assert "# Slack Message" in result["content"]

    def test_content_includes_source_key(self):
        result = self._build()
        assert "Source Key:" in result["content"]

    def test_document_type_is_message(self):
        result = self._build()
        assert result["document_type"] == "message"

    def test_snippet_generated(self):
        result = self._build(text="hello team")
        assert result["snippet"] == "hello team"


# ── build_thread_file ─────────────────────────────────────────────────────
class TestBuildThreadFile:
    def _build(self, replies=None):
        from ingestion.ingest_slack import build_thread_file
        parent = {"ts": "100.0", "user": "U001", "text": "parent message"}
        replies = replies or [
            {"ts": "100.0", "user": "U001", "text": "parent message"},  # Slack includes parent
            {"ts": "200.0", "user": "U002", "text": "reply text"},
        ]
        return build_thread_file(parent, replies, "C123", "general", _mock_slack())

    def test_returns_required_keys(self):
        result = self._build()
        for key in ("filename", "content", "stable_key", "document_type"):
            assert key in result

    def test_stable_key_thread_format(self):
        result = self._build()
        assert result["stable_key"].startswith("slack_thread:C123:")

    def test_content_has_thread_header(self):
        result = self._build()
        assert "# Slack Thread" in result["content"]

    def test_parent_not_duplicated(self):
        result = self._build()
        # Parent at ts=100.0 is filtered; only the reply should appear under Replies
        assert result["content"].count("parent message") == 1

    def test_document_type_is_thread(self):
        result = self._build()
        assert result["document_type"] == "thread"

    def test_reply_included_in_content(self):
        result = self._build()
        assert "reply text" in result["content"]


# ── process_channel ───────────────────────────────────────────────────────
class TestProcessChannel:
    def _state(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState
        return IngestionState(tmp_state_path)

    def test_no_messages_returns_empty_files(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel
        slack = _mock_slack()
        slack.fetch_channel_messages.return_value = []
        result = process_channel(slack, "C123", self._state(tmp_state_path), force=False)
        assert result["files"] == []

    def test_standalone_message_included(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel
        slack = _mock_slack()
        slack.fetch_channel_messages.return_value = [
            _msg("100.0", "hello team"),
        ]
        result = process_channel(slack, "C123", self._state(tmp_state_path), force=False)
        assert len(result["files"]) == 1

    def test_thread_parent_fetches_replies(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel
        slack = _mock_slack()
        parent = _msg("100.0", "parent", reply_count=2, thread_ts="100.0")
        slack.fetch_channel_messages.return_value = [parent]
        slack.fetch_thread_replies.return_value = [
            _msg("100.0", "parent"),
            _msg("200.0", "reply"),
        ]
        result = process_channel(slack, "C123", self._state(tmp_state_path), force=False)
        assert result["thread_count"] == 1
        slack.fetch_thread_replies.assert_called_once()

    def test_thread_reply_skipped(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel
        slack = _mock_slack()
        slack.fetch_channel_messages.return_value = [
            _msg("200.0", "reply", thread_ts="100.0"),  # reply, not parent
        ]
        result = process_channel(slack, "C123", self._state(tmp_state_path), force=False)
        assert len(result["files"]) == 0

    def test_noise_message_skipped(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel
        slack = _mock_slack()
        slack.fetch_channel_messages.return_value = [
            _msg("100.0", "", subtype="channel_join"),
        ]
        result = process_channel(slack, "C123", self._state(tmp_state_path), force=False)
        assert len(result["files"]) == 0

    def test_already_uploaded_skipped(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel
        from ingestion.ingestion_state import IngestionState
        state = IngestionState(tmp_state_path)
        state.mark_uploaded("slack:C123:100.0", "f.md", "C123", "general")
        state.save()

        slack = _mock_slack()
        slack.fetch_channel_messages.return_value = [_msg("100.0", "hello")]
        result = process_channel(slack, "C123", IngestionState(tmp_state_path), force=False)
        assert result["skipped_count"] == 1
        assert len(result["files"]) == 0

    def test_force_reingest_ignores_existing(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel
        from ingestion.ingestion_state import IngestionState
        state = IngestionState(tmp_state_path)
        state.mark_uploaded("slack:C123:100.0", "f.md", "C123", "general")
        state.save()

        slack = _mock_slack()
        slack.fetch_channel_messages.return_value = [_msg("100.0", "hello")]
        result = process_channel(slack, "C123", IngestionState(tmp_state_path), force=True)
        assert result["skipped_count"] == 0
        assert len(result["files"]) == 1


# ── _record_successful_uploads ────────────────────────────────────────────
class TestRecordSuccessfulUploads:
    def _batch(self):
        return [{
            "filename": "file1.md",
            "content": "content",
            "stable_key": "slack:C1:100.0",
            "channel_id": "C1",
            "channel_name": "general",
            "ts": "100.0",
            "thread_ts": None,
            "user_name": "Alice",
            "timestamp": "100.0",
            "snippet": "hello",
            "permalink": "https://slack.com/p",
            "document_type": "message",
        }]

    def test_records_per_file_results(self, tmp_state_path):
        from ingestion.ingest_slack import _record_successful_uploads
        from ingestion.ingestion_state import IngestionState
        state = IngestionState(tmp_state_path)
        batch = self._batch()
        response = {
            "results": [{"filename": "file1.md", "status": "queued", "source_id": "doc-abc"}]
        }
        recorded = _record_successful_uploads(state, batch, response)
        assert recorded == 1
        assert state.has("slack:C1:100.0")
        assert state.get("slack:C1:100.0")["source_id"] == "doc-abc"

    def test_failed_result_not_recorded(self, tmp_state_path):
        from ingestion.ingest_slack import _record_successful_uploads
        from ingestion.ingestion_state import IngestionState
        state = IngestionState(tmp_state_path)
        batch = self._batch()
        response = {
            "results": [{"filename": "file1.md", "status": "failed", "error": "oops"}]
        }
        recorded = _record_successful_uploads(state, batch, response)
        assert recorded == 0
        assert not state.has("slack:C1:100.0")

    def test_fallback_when_no_results_key(self, tmp_state_path):
        from ingestion.ingest_slack import _record_successful_uploads
        from ingestion.ingestion_state import IngestionState
        state = IngestionState(tmp_state_path)
        batch = self._batch()
        # Response without 'results' but with success_count
        response = {"success": True, "success_count": 1}
        recorded = _record_successful_uploads(state, batch, response)
        assert recorded == 1


# ── fetch_channel_name ────────────────────────────────────────────────────
class TestFetchChannelName:
    def test_returns_channel_name(self):
        from ingestion.ingest_slack import fetch_channel_name
        slack = _mock_slack()
        slack.client.conversations_info.return_value = {"channel": {"name": "product"}}
        assert fetch_channel_name(slack, "C123") == "product"

    def test_falls_back_to_channel_id_on_error(self):
        from ingestion.ingest_slack import fetch_channel_name
        from slack_sdk.errors import SlackApiError
        slack = _mock_slack()
        slack.client.conversations_info.side_effect = SlackApiError(
            message="not_in_channel",
            response={"error": "not_in_channel"},
        )
        assert fetch_channel_name(slack, "C123") == "C123"
