"""Tests for recall.py — chunk extraction, filtering, citation hygiene."""

import pytest


# ── _extract_chunks ───────────────────────────────────────────────────────
class TestExtractChunks:
    def _extract(self, payload):
        from recall import _extract_chunks
        return _extract_chunks(payload)

    def test_empty_dict_returns_empty(self):
        assert self._extract({}) == []

    def test_none_returns_empty(self):
        assert self._extract(None) == []

    def test_chunks_key(self):
        chunks = [{"text": "a"}, {"text": "b"}]
        assert self._extract({"chunks": chunks}) == chunks

    def test_results_key(self):
        chunks = [{"text": "x"}]
        assert self._extract({"results": chunks}) == chunks

    def test_documents_key(self):
        chunks = [{"text": "y"}]
        assert self._extract({"documents": chunks}) == chunks

    def test_nested_data_chunks(self):
        chunks = [{"text": "nested"}]
        assert self._extract({"data": {"chunks": chunks}}) == chunks

    def test_non_list_value_skipped(self):
        assert self._extract({"chunks": "not a list"}) == []

    def test_prefers_first_matching_key(self):
        chunks_a = [{"text": "a"}]
        chunks_b = [{"text": "b"}]
        result = self._extract({"chunks": chunks_a, "results": chunks_b})
        assert result == chunks_a


# ── _chunk_text ────────────────────────────────────────────────────────────
class TestChunkText:
    def _text(self, chunk):
        from recall import _chunk_text
        return _chunk_text(chunk)

    def test_string_chunk_returned_directly(self):
        assert self._text("plain text") == "plain text"

    def test_text_key(self):
        assert self._text({"text": "hello"}) == "hello"

    def test_content_key(self):
        assert self._text({"content": "world"}) == "world"

    def test_page_content_key(self):
        assert self._text({"page_content": "content here"}) == "content here"

    def test_nested_payload_text(self):
        assert self._text({"payload": {"text": "deep text"}}) == "deep text"

    def test_recursive_fallback(self):
        chunk = {"some_field": "short", "body_text": "this is a long enough body text to pass the minimum length check"}
        result = self._text(chunk)
        assert "long enough" in result

    def test_empty_dict_returns_empty(self):
        assert self._text({}) == ""

    def test_stripping(self):
        assert self._text({"text": "  hello  "}) == "hello"

    def test_list_of_strings_joined(self):
        assert self._text({"text": ["line1", "line2"]}) == "line1\nline2"


# ── _chunk_source ──────────────────────────────────────────────────────────
class TestChunkSource:
    def _source(self, chunk, index=0):
        from recall import _chunk_source
        return _chunk_source(chunk, index)

    def test_source_id_key(self):
        assert self._source({"source_id": "doc-123"}) == "doc-123"

    def test_filename_key(self):
        assert self._source({"filename": "file.md"}) == "file.md"

    def test_fallback_to_chunk_n(self):
        assert self._source({}, index=5) == "chunk_5"

    def test_nested_metadata_filename(self):
        assert self._source({"metadata": {"filename": "nested.md"}}) == "nested.md"


# ── _coerce_to_unix_seconds ───────────────────────────────────────────────
class TestCoerceToUnixSeconds:
    def _coerce(self, value):
        from recall import _coerce_to_unix_seconds
        return _coerce_to_unix_seconds(value)

    def test_none_returns_none(self):
        assert self._coerce(None) is None

    def test_int(self):
        assert self._coerce(1000000000) == 1000000000.0

    def test_float(self):
        assert abs(self._coerce(1000000000.5) - 1000000000.5) < 0.001

    def test_slack_ts_string(self):
        assert abs(self._coerce("1778775842.876209") - 1778775842.876209) < 0.001

    def test_invalid_string_returns_none(self):
        assert self._coerce("not-a-number") is None

    def test_empty_string_returns_none(self):
        assert self._coerce("") is None


# ── _source_passes_filters ────────────────────────────────────────────────
class TestSourcePassesFilters:
    def _passes(self, source_card, **kwargs):
        from recall import _source_passes_filters
        return _source_passes_filters(source_card, **kwargs)

    def _card(self, **kwargs):
        defaults = {"channel": "general", "user": "Alice", "document_type": "message", "timestamp": "1000000000.0"}
        defaults.update(kwargs)
        return defaults

    def test_no_filters_passes(self):
        assert self._passes(self._card(), channel=None, user=None, document_type=None)

    def test_channel_filter_match(self):
        assert self._passes(self._card(channel="general"), channel="general", user=None, document_type=None)

    def test_channel_filter_no_match(self):
        assert not self._passes(self._card(channel="engineering"), channel="general", user=None, document_type=None)

    def test_channel_filter_case_insensitive(self):
        assert self._passes(self._card(channel="General"), channel="general", user=None, document_type=None)

    def test_user_filter_match(self):
        assert self._passes(self._card(user="Alice"), channel=None, user="Alice", document_type=None)

    def test_user_filter_no_match(self):
        assert not self._passes(self._card(user="Bob"), channel=None, user="Alice", document_type=None)

    def test_document_type_filter(self):
        assert not self._passes(self._card(document_type="thread"), channel=None, user=None, document_type="message")

    def test_timestamp_filter_in_range(self):
        assert self._passes(
            self._card(timestamp="1000000500.0"),
            channel=None, user=None, document_type=None,
            start_unix=1000000000.0, end_unix=1000001000.0,
        )

    def test_timestamp_filter_before_start(self):
        assert not self._passes(
            self._card(timestamp="999999999.0"),
            channel=None, user=None, document_type=None,
            start_unix=1000000000.0, end_unix=None,
        )

    def test_timestamp_filter_after_end(self):
        assert not self._passes(
            self._card(timestamp="1000001001.0"),
            channel=None, user=None, document_type=None,
            start_unix=None, end_unix=1000001000.0,
        )

    def test_card_without_timestamp_passes_date_filter(self):
        """Cards with no parseable timestamp are let through to avoid over-filtering."""
        card = {"channel": "general"}
        assert self._passes(card, channel=None, user=None, document_type=None, start_unix=1e9, end_unix=2e9)


# ── _strip_invalid_citations ──────────────────────────────────────────────
class TestStripInvalidCitations:
    def _strip(self, answer, allowed):
        from recall import _strip_invalid_citations
        return _strip_invalid_citations(answer, set(allowed))

    def test_valid_citation_kept(self):
        result = self._strip("The answer is here [1].", [1])
        assert "[1]" in result

    def test_invalid_citation_removed(self):
        result = self._strip("See [3] for details.", [1, 2])
        assert "[3]" not in result

    def test_mixed_citations(self):
        result = self._strip("First [1] and second [2] and third [3].", [1, 2])
        assert "[1]" in result
        assert "[2]" in result
        assert "[3]" not in result

    def test_cjk_citation_stripped(self):
        result = self._strip("See 【2】 for details.", [1])
        assert "【2】" not in result

    def test_cjk_citation_with_source_stripped(self):
        result = self._strip("See 【1†source: file.md】 for details.", [2])
        assert "†" not in result

    def test_empty_allowed_strips_all(self):
        result = self._strip("No context [1] [2] [3].", [])
        assert "[1]" not in result
        assert "[2]" not in result
        assert "[3]" not in result

    def test_empty_answer_returned_unchanged(self):
        assert self._strip("", [1, 2]) == ""

    def test_no_citations_unchanged(self):
        text = "No citations here."
        assert self._strip(text, [1, 2]) == text


# ── _clean_sources_for_ui ─────────────────────────────────────────────────
class TestCleanSourcesForUI:
    def _rich(self, idx, stable_key=None, permalink=None):
        return {
            "index": idx,
            "source": f"src_{idx}",
            "channel": "general",
            "user": "Alice",
            "snippet": "text",
            "stable_key": stable_key or f"key:{idx}",
            "permalink": permalink or f"https://slack.com/{idx}",
        }

    def _minimal(self, idx, source=None):
        return {"index": idx, "source": source or f"doc_{idx}", "score": 0.9}

    def test_empty_list_returns_empty(self):
        from recall import _clean_sources_for_ui
        assert _clean_sources_for_ui([], top_k=5) == []

    def test_caps_at_top_k(self):
        from recall import _clean_sources_for_ui
        sources = [self._rich(i, stable_key=f"k{i}") for i in range(10)]
        result = _clean_sources_for_ui(sources, top_k=3)
        assert len(result) <= 3

    def test_rich_over_minimal(self):
        from recall import _clean_sources_for_ui
        sources = [self._minimal(1), self._rich(2)]
        result = _clean_sources_for_ui(sources, top_k=5)
        # Rich sources are preferred when at least one exists
        assert all(s.get("channel") is not None for s in result)

    def test_dedupe_by_stable_key(self):
        from recall import _clean_sources_for_ui
        sources = [
            self._rich(1, stable_key="same_key"),
            self._rich(2, stable_key="same_key"),
        ]
        result = _clean_sources_for_ui(sources, top_k=5)
        assert len(result) == 1
        assert result[0]["index"] == 1  # first wins

    def test_fallback_to_minimal_when_all_minimal(self):
        from recall import _clean_sources_for_ui
        sources = [self._minimal(1), self._minimal(2)]
        result = _clean_sources_for_ui(sources, top_k=5)
        assert len(result) == 2


# ── finalize_answer ───────────────────────────────────────────────────────
class TestFinalizeAnswer:
    def _rich_source(self, idx, stable_key=None):
        return {
            "index": idx,
            "source": "general",
            "channel": "general",
            "user": "Alice",
            "snippet": "snippet",
            "stable_key": stable_key or f"key:{idx}",
            "permalink": f"https://slack.com/{idx}",
        }

    def test_returns_required_keys(self):
        from recall import finalize_answer
        result = finalize_answer("answer [1].", [self._rich_source(1)], top_k=5)
        assert set(result.keys()) >= {"answer", "cleaned_sources", "sources_before", "sources_after"}

    def test_invalid_citation_stripped(self):
        from recall import finalize_answer
        sources = [self._rich_source(1)]
        result = finalize_answer("see [1] and [99]", sources, top_k=5)
        assert "[99]" not in result["answer"]
        assert "[1]" in result["answer"]

    def test_source_counts_reported(self):
        from recall import finalize_answer
        sources = [self._rich_source(i, stable_key=f"k{i}") for i in range(4)]
        result = finalize_answer("answer", sources, top_k=2)
        assert result["sources_before"] == 4
        assert result["sources_after"] <= 2
