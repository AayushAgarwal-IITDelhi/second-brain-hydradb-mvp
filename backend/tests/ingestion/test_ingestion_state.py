"""Tests for ingestion/ingestion_state.py — state persistence and lookups."""

import json
import os
from pathlib import Path

import pytest


# ── stable key helpers ────────────────────────────────────────────────────
class TestStableKeys:
    def test_message_key_format(self):
        from ingestion.ingestion_state import stable_key_for_message

        assert stable_key_for_message("C123", "100.0") == "slack:C123:100.0"

    def test_thread_key_format(self):
        from ingestion.ingestion_state import stable_key_for_thread

        assert stable_key_for_thread("C123", "100.0") == "slack_thread:C123:100.0"

    def test_message_key_unique_per_ts(self):
        from ingestion.ingestion_state import stable_key_for_message

        k1 = stable_key_for_message("C123", "100.0")
        k2 = stable_key_for_message("C123", "200.0")
        assert k1 != k2

    def test_thread_key_unique_per_channel(self):
        from ingestion.ingestion_state import stable_key_for_thread

        k1 = stable_key_for_thread("C123", "100.0")
        k2 = stable_key_for_thread("C456", "100.0")
        assert k1 != k2


# ── load / save ───────────────────────────────────────────────────────────
class TestIngestionStateIO:
    def test_empty_state_on_missing_file(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        assert state.entries == {}
        assert state.channels == {}

    def test_save_and_reload(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        state.mark_uploaded(
            stable_key="slack:C123:100.0",
            filename="file.md",
            channel_id="C123",
            channel_name="general",
            ts="100.0",
        )
        state.save()

        reloaded = IngestionState(tmp_state_path)
        assert reloaded.has("slack:C123:100.0")

    def test_save_is_atomic(self, tmp_state_path):
        """After save, no .tmp file should remain."""
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        state.save()
        tmp = tmp_state_path.with_suffix(".json.tmp")
        assert not tmp.exists()

    def test_corrupt_file_starts_empty(self, tmp_path):
        from ingestion.ingestion_state import IngestionState

        p = tmp_path / "corrupt.json"
        p.write_text("{{not valid json")
        state = IngestionState(p)
        assert state.entries == {}

    def test_version_written_to_file(self, tmp_state_path):
        from ingestion.ingestion_state import STATE_VERSION, IngestionState

        state = IngestionState(tmp_state_path)
        state.save()
        raw = json.loads(tmp_state_path.read_text())
        assert raw["version"] == STATE_VERSION

    def test_old_format_without_channels_loads(self, tmp_path):
        from ingestion.ingestion_state import IngestionState

        p = tmp_path / "old.json"
        p.write_text(json.dumps({"version": 1, "entries": {}}))
        state = IngestionState(p)
        assert state.channels == {}


# ── has / mark_uploaded ───────────────────────────────────────────────────
class TestHasAndMarkUploaded:
    def test_has_returns_false_for_unknown(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        assert state.has("unknown:key") is False

    def test_has_returns_true_after_mark(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        state.mark_uploaded("slack:C1:1.0", "f.md", "C1", "general")
        assert state.has("slack:C1:1.0") is True

    def test_mark_uploaded_stores_metadata(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        state.mark_uploaded(
            stable_key="slack:C1:1.0",
            filename="file.md",
            channel_id="C1",
            channel_name="general",
            ts="1.0",
            source_id="doc-123",
            user_name="Alice",
            snippet="Hello",
            permalink="https://slack.com/p/1",
            document_type="message",
        )
        entry = state.get("slack:C1:1.0")
        assert entry["source_id"] == "doc-123"
        assert entry["user_name"] == "Alice"
        assert entry["snippet"] == "Hello"
        assert entry["document_type"] == "message"

    def test_mark_uploaded_sets_uploaded_at(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        state.mark_uploaded("k", "f.md", "C1", "general")
        assert state.get("k")["uploaded_at"] is not None


# ── lookups ───────────────────────────────────────────────────────────────
class TestLookups:
    def test_find_by_source_id(self, populated_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(populated_state_path)
        entry = state.find_by_source_id("doc-001")
        assert entry is not None
        assert entry["channel_name"] == "general"

    def test_find_by_source_id_missing_returns_none(self, populated_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(populated_state_path)
        assert state.find_by_source_id("nonexistent-id") is None

    def test_find_by_source_id_empty_string_returns_none(self, populated_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(populated_state_path)
        assert state.find_by_source_id("") is None

    def test_find_by_filename(self, populated_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(populated_state_path)
        entry = state.find_by_filename("slack_general_1000000001.md")
        assert entry is not None
        assert entry["stable_key"] == "slack:C123:1000000001.000000"

    def test_find_by_filename_missing_returns_none(self, populated_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(populated_state_path)
        assert state.find_by_filename("ghost.md") is None

    def test_total_docs(self, populated_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(populated_state_path)
        assert state.total_docs() == 2


# ── watermarks ────────────────────────────────────────────────────────────
class TestWatermarks:
    def test_get_last_synced_ts_none_initially(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        assert state.get_last_synced_ts("C123") is None

    def test_set_and_get_last_synced_ts(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        state.set_last_synced_ts("C123", "1000000500.000000")
        assert state.get_last_synced_ts("C123") == "1000000500.000000"

    def test_watermark_does_not_go_backward(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        state.set_last_synced_ts("C123", "1000000500.000000")
        state.set_last_synced_ts("C123", "1000000100.000000")  # older — should be ignored
        assert state.get_last_synced_ts("C123") == "1000000500.000000"

    def test_watermark_advances(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        state.set_last_synced_ts("C123", "1000000100.000000")
        state.set_last_synced_ts("C123", "1000000500.000000")  # newer
        assert state.get_last_synced_ts("C123") == "1000000500.000000"

    def test_empty_channel_id_ignored(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        state.set_last_synced_ts("", "100.0")  # should not crash
        assert state.get_last_synced_ts("") is None

    def test_touch_and_get_last_ingested_at(self, tmp_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(tmp_state_path)
        assert state.get_last_ingested_at() is None
        state.touch_last_ingested()
        assert state.get_last_ingested_at() is not None

    def test_get_last_ingested_at_from_populated_state(self, populated_state_path):
        from ingestion.ingestion_state import IngestionState

        state = IngestionState(populated_state_path)
        ts = state.get_last_ingested_at()
        assert ts == "2026-01-02T00:00:00+00:00"
