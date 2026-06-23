"""
Integration tests: Slack ingestion → normalization → upload pipeline.

Flow 3: Raw Slack messages → normalize → build docs → upload → state recorded.
All Slack API and HydraDB HTTP calls are mocked.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


def _msg(ts, text, user="U001", reply_count=0, thread_ts=None):
    m = {"ts": ts, "text": text, "user": user, "reply_count": reply_count}
    if thread_ts:
        m["thread_ts"] = thread_ts
    return m


def _hydra_success(filename):
    return {
        "success": True,
        "success_count": 1,
        "failed_count": 0,
        "results": [{"filename": filename, "status": "queued", "source_id": "hydra-doc-1"}],
    }


def _mock_slack_wrapper():
    slack = MagicMock()
    slack.client.conversations_info.return_value = {"channel": {"name": "general"}}
    slack.client.auth_test.return_value = {"user_id": "BOT_USER"}
    slack.resolve_user_name.return_value = "Alice"
    slack.get_permalink.return_value = "https://slack.com/perma"
    return slack


# ── Full ingestion pipeline ────────────────────────────────────────────────
class TestIngestionPipeline:
    def test_message_ingested_and_state_saved(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel, upload_in_batches
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        slack = _mock_slack_wrapper()
        slack.fetch_channel_messages.return_value = [_msg("100.0", "hello team")]
        slack.fetch_thread_replies.return_value = []

        result = process_channel(slack, "C123", state, force=False)
        assert len(result["files"]) == 1

        mock_hydra = MagicMock()
        mock_hydra.upload_knowledge.return_value = _hydra_success(result["files"][0]["filename"])
        stats = upload_in_batches(mock_hydra, result["files"], state)

        assert stats["successes"] == 1
        assert stats["failures"] == 0
        assert state.has("slack:C123:100.0")

    def test_thread_ingested_with_replies(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel, upload_in_batches
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        slack = _mock_slack_wrapper()
        parent = _msg("100.0", "parent", reply_count=2, thread_ts="100.0")
        slack.fetch_channel_messages.return_value = [parent]
        slack.fetch_thread_replies.return_value = [
            _msg("100.0", "parent"),
            _msg("200.0", "reply 1"),
            _msg("300.0", "reply 2"),
        ]

        result = process_channel(slack, "C123", state, force=False)
        assert result["thread_count"] == 1
        thread_file = result["files"][0]
        assert "reply 1" in thread_file["content"]
        assert "reply 2" in thread_file["content"]

    def test_force_reingest_reuploads_existing(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel
        from ingestion.ingestion_state import IngestionState, stable_key_for_message

        state = IngestionState(tmp_state_path)
        key = stable_key_for_message("C123", "100.0")
        state.mark_uploaded(key, "f.md", "C123", "general")
        state._save()

        state2 = IngestionState(tmp_state_path)
        slack = _mock_slack_wrapper()
        slack.fetch_channel_messages.return_value = [_msg("100.0", "existing message")]

        result = process_channel(slack, "C123", state2, force=True)
        assert result["skipped_count"] == 0
        assert len(result["files"]) == 1

    def test_incremental_uses_last_synced_ts(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        state.set_last_synced_ts("C123", "100.0")
        state._save()

        state2 = IngestionState(tmp_state_path)
        slack = _mock_slack_wrapper()
        slack.fetch_channel_messages.return_value = []

        process_channel(slack, "C123", state2, force=False)
        # fetch_channel_messages called with oldest=100.0
        call_kwargs = slack.fetch_channel_messages.call_args
        assert call_kwargs[1].get("oldest") == "100.0" or call_kwargs[0][2] == "100.0"

    def test_upload_failure_records_zero(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel, upload_in_batches
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        slack = _mock_slack_wrapper()
        slack.fetch_channel_messages.return_value = [_msg("100.0", "hello")]

        result = process_channel(slack, "C123", state, force=False)
        assert len(result["files"]) == 1

        mock_hydra = MagicMock()
        mock_hydra.upload_knowledge.return_value = {"success": False}
        stats = upload_in_batches(mock_hydra, result["files"], state)

        assert stats["failures"] > 0 or stats["successes"] == 0
        assert not state.has("slack:C123:100.0")

    def test_state_persisted_after_upload(self, tmp_state_path):
        """State is written to disk after a successful upload."""
        from ingestion.ingest_slack import process_channel, upload_in_batches
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        slack = _mock_slack_wrapper()
        slack.fetch_channel_messages.return_value = [_msg("100.0", "hi")]

        result = process_channel(slack, "C123", state, force=False)
        fn = result["files"][0]["filename"]

        mock_hydra = MagicMock()
        mock_hydra.upload_knowledge.return_value = _hydra_success(fn)
        upload_in_batches(mock_hydra, result["files"], state)

        # Reload from disk to verify persistence
        reloaded = IngestionState(tmp_state_path)
        assert reloaded.has("slack:C123:100.0")

    def test_noise_messages_not_uploaded(self, tmp_state_path):
        from ingestion.ingest_slack import process_channel
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        slack = _mock_slack_wrapper()
        slack.fetch_channel_messages.return_value = [
            {"ts": "100.0", "text": "", "subtype": "channel_join"},
            {"ts": "200.0", "text": "real message", "user": "U001"},
        ]
        result = process_channel(slack, "C123", state, force=False)
        assert len(result["files"]) == 1  # only real message


# ── Realtime ingestion (standalone message path) ───────────────────────────
class TestRealtimeIngestionStandalone:
    def test_standalone_event_uploaded(self, tmp_state_path):
        from realtime_ingest import _ingest_standalone

        slack = _mock_slack_wrapper()
        hydra = MagicMock()
        hydra.upload_knowledge.return_value = {
            "success": True,
            "success_count": 1,
            "results": [{"filename": "slack_general_100.md", "status": "queued", "source_id": "doc-x"}],
        }

        event = {"ts": "100.0", "user": "U001", "text": "hello from realtime"}
        with patch("realtime_ingest.STATE_PATH", tmp_state_path):
            _ingest_standalone(slack, hydra, "C123", "general", event)

    def test_duplicate_event_skipped(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState
        from realtime_ingest import _ingest_standalone

        state = IngestionState(tmp_state_path)
        state.mark_uploaded("slack:C123:100.0", "f.md", "C123", "general")
        state._save()

        slack = _mock_slack_wrapper()
        hydra = MagicMock()

        event = {"ts": "100.0", "user": "U001", "text": "dup event"}
        with patch("realtime_ingest.STATE_PATH", tmp_state_path):
            _ingest_standalone(slack, hydra, "C123", "general", event)

        hydra.upload_knowledge.assert_not_called()


# ── Normalization fidelity ────────────────────────────────────────────────
class TestNormalizationFidelity:
    """Verify that build_message_file / build_thread_file produce correct markdown."""

    def test_message_file_contains_all_header_fields(self):
        from ingestion.ingest_slack import build_message_file

        slack = _mock_slack_wrapper()
        msg = {"ts": "1000000001.000000", "user": "U001", "text": "content here"}
        result = build_message_file(msg, "C123", "general", slack)
        content = result["content"]
        assert "# Slack Message" in content
        assert "Source Key:" in content
        assert "Channel:" in content
        assert "Timestamp:" in content
        assert "User:" in content

    def test_thread_file_contains_parent_and_replies(self):
        from ingestion.ingest_slack import build_thread_file

        slack = _mock_slack_wrapper()
        parent = {"ts": "100.0", "user": "U001", "text": "parent"}
        replies = [
            {"ts": "100.0", "user": "U001", "text": "parent"},
            {"ts": "200.0", "user": "U002", "text": "reply here"},
        ]
        result = build_thread_file(parent, replies, "C123", "general", slack)
        content = result["content"]
        assert "Parent:" in content
        assert "Replies:" in content
        assert "reply here" in content

    def test_filename_collision_between_message_and_thread(self):
        """
        BUG CHECK: message file and thread file for the same ts produce
        the same filename. This can cause HydraDB to overwrite one with the other.
        """
        from ingestion.ingest_slack import build_message_file, build_thread_file

        slack = _mock_slack_wrapper()
        ts = "1000000001.000000"
        msg = {"ts": ts, "user": "U001", "text": "standalone message"}
        parent = {"ts": ts, "user": "U001", "text": "thread parent"}
        replies = [parent, {"ts": "200.0", "user": "U002", "text": "reply"}]

        msg_file = build_message_file(msg, "C123", "general", slack)
        thread_file = build_thread_file(parent, replies, "C123", "general", slack)

        # Filenames must differ: build_message_file uses _msg_ and
        # build_thread_file uses _thread_ so HydraDB uploads never collide.
        assert msg_file["filename"] != thread_file["filename"], f"Filename collision: both are {msg_file['filename']!r}"
        assert "_msg_" in msg_file["filename"]
        assert "_thread_" in thread_file["filename"]
