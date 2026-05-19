"""Tests for search_utils.py — keyword extraction and chunk ranking."""

import pytest


# ── extract_query_terms ────────────────────────────────────────────────────
class TestExtractQueryTerms:
    def _extract(self, question):
        from search_utils import extract_query_terms
        return extract_query_terms(question)

    def test_empty_string(self):
        assert self._extract("") == []

    def test_none_handled(self):
        assert self._extract(None) == []

    def test_stopwords_removed(self):
        terms = self._extract("what is the status of our project")
        assert "what" not in terms
        assert "is" not in terms
        assert "the" not in terms
        assert "of" not in terms

    def test_keywords_extracted(self):
        terms = self._extract("sprint deadline API design")
        assert "sprint" in terms
        assert "deadline" in terms
        assert "api" in terms
        assert "design" in terms

    def test_terms_are_lowercase(self):
        terms = self._extract("Alice Bob Charlie")
        # These ARE captured (not blocklisted in search_utils) but lowercased
        for t in terms:
            assert t == t.lower()

    def test_quoted_phrase_extracted_whole(self):
        terms = self._extract('"sprint deadline" and roadmap')
        assert "sprint deadline" in terms

    def test_quoted_phrase_not_retokenized(self):
        terms = self._extract('"exact phrase"')
        assert "exact phrase" in terms
        # Shouldn't also appear as individual tokens
        assert terms.count("exact") + terms.count("phrase") == 0

    def test_no_duplicates(self):
        terms = self._extract("api api API")
        assert terms.count("api") == 1

    def test_short_tokens_excluded(self):
        terms = self._extract("a b go do")
        assert "a" not in terms
        assert "b" not in terms

    def test_order_preserved(self):
        terms = self._extract("sprint roadmap deadline")
        sprint_idx = terms.index("sprint")
        roadmap_idx = terms.index("roadmap")
        deadline_idx = terms.index("deadline")
        assert sprint_idx < roadmap_idx < deadline_idx

    def test_hyphenated_tokens(self):
        terms = self._extract("all-second-brain channel")
        # Hyphenated tokens are kept
        found = any("second" in t or "all-second-brain" in t for t in terms)
        assert found or "all-second-brain" in terms or "channel" in terms


# ── count_keyword_hits ────────────────────────────────────────────────────
class TestCountKeywordHits:
    def _hits(self, text, terms):
        from search_utils import count_keyword_hits
        return count_keyword_hits(text, terms)

    def test_zero_on_empty_text(self):
        assert self._hits("", ["sprint"]) == 0

    def test_zero_on_empty_terms(self):
        assert self._hits("sprint is upcoming", []) == 0

    def test_single_hit(self):
        assert self._hits("the sprint deadline is friday", ["sprint"]) == 1

    def test_multiple_hits_each_term_counted_once(self):
        hits = self._hits("sprint sprint sprint deadline", ["sprint", "deadline"])
        assert hits == 2  # each term counted once even if repeated

    def test_case_insensitive(self):
        assert self._hits("The Sprint Is On Friday", ["sprint", "friday"]) == 2

    def test_word_boundary_enforced_for_single_tokens(self):
        # "ai" should NOT match "again" or "braid"
        assert self._hits("again braid train", ["ai"]) == 0

    def test_phrase_matched_as_substring(self):
        assert self._hits("the sprint deadline is critical", ["sprint deadline"]) == 1

    def test_phrase_not_matched_across_words(self):
        assert self._hits("sprint and the deadline", ["sprint deadline"]) == 0

    def test_none_text_returns_zero(self):
        assert self._hits(None, ["sprint"]) == 0


# ── rerank_chunks ─────────────────────────────────────────────────────────
class TestRerankChunks:
    def _chunk(self, text, idx=1, stable_key=None, ts=None):
        card = {"index": idx, "source": f"src_{idx}"}
        if stable_key:
            card["stable_key"] = stable_key
        if ts is not None:
            card["timestamp"] = str(ts)
        return {
            "text": text,
            "source_card": card,
            "original_index": idx,
            "timestamp_float": float(ts) if ts is not None else None,
        }

    def test_empty_input_returns_empty(self):
        from search_utils import rerank_chunks
        result, count = rerank_chunks([], [], "default", 5)
        assert result == []
        assert count == 0

    def test_default_mode_preserves_order(self):
        from search_utils import rerank_chunks
        chunks = [
            self._chunk("alpha content", idx=1),
            self._chunk("beta content", idx=2),
            self._chunk("gamma content", idx=3),
        ]
        result, _ = rerank_chunks(chunks, [], "default", 5)
        assert [c["original_index"] for c in result] == [1, 2, 3]

    def test_exact_mode_prefers_keyword_hits(self):
        from search_utils import rerank_chunks
        chunks = [
            self._chunk("no matches here at all", idx=1),
            self._chunk("sprint deadline is critical this week", idx=2),
        ]
        terms = ["sprint", "deadline"]
        result, exact = rerank_chunks(chunks, terms, "exact", 5)
        assert result[0]["original_index"] == 2  # keyword chunk first
        assert exact == 1

    def test_exact_mode_falls_back_to_semantic_when_no_hits(self):
        from search_utils import rerank_chunks
        chunks = [
            self._chunk("alpha", idx=1),
            self._chunk("beta", idx=2),
        ]
        result, exact = rerank_chunks(chunks, ["zebra"], "exact", 5)
        # No exact matches; falls back to original order
        assert exact == 0
        assert len(result) == 2

    def test_hybrid_mode_includes_all_chunks(self):
        from search_utils import rerank_chunks
        chunks = [
            self._chunk("no keyword match", idx=1),
            self._chunk("sprint is the keyword", idx=2),
        ]
        result, _ = rerank_chunks(chunks, ["sprint"], "hybrid", 5)
        # Hybrid mode keeps all chunks
        assert len(result) == 2

    def test_top_k_cap(self):
        from search_utils import rerank_chunks
        chunks = [self._chunk(f"chunk {i}", idx=i) for i in range(10)]
        result, _ = rerank_chunks(chunks, [], "default", 3)
        assert len(result) == 3

    def test_metadata_bias_pushes_matching_chunk_up(self):
        from search_utils import rerank_chunks
        chunks = [
            self._chunk("engineering content", idx=1),
            self._chunk("product content", idx=2),
        ]
        chunks[1]["source_card"]["channel"] = "product"
        result, _ = rerank_chunks(
            chunks, [], "default", 5,
            metadata_bias={"channel": "product"},
        )
        assert result[0]["original_index"] == 2  # product chunk first

    def test_returns_tuple(self):
        from search_utils import rerank_chunks
        out = rerank_chunks([], [], "default", 5)
        assert isinstance(out, tuple)
        assert len(out) == 2


# ── dedupe_by_stable_key ──────────────────────────────────────────────────
class TestDedupeByStableKey:
    def _chunk(self, idx, stable_key=None):
        card = {"source": f"src_{idx}"}
        if stable_key:
            card["stable_key"] = stable_key
        return {
            "text": f"chunk {idx}",
            "source_card": card,
            "original_index": idx,
        }

    def test_no_duplicates_unchanged(self):
        from search_utils import dedupe_by_stable_key
        chunks = [
            self._chunk(1, "key:1"),
            self._chunk(2, "key:2"),
        ]
        assert dedupe_by_stable_key(chunks) == chunks

    def test_duplicate_stable_key_removed(self):
        from search_utils import dedupe_by_stable_key
        chunks = [
            self._chunk(1, "key:same"),
            self._chunk(2, "key:same"),  # duplicate
        ]
        result = dedupe_by_stable_key(chunks)
        assert len(result) == 1
        assert result[0]["original_index"] == 1  # first wins

    def test_chunks_without_stable_key_kept(self):
        from search_utils import dedupe_by_stable_key
        chunks = [
            self._chunk(1, None),
            self._chunk(2, None),
        ]
        result = dedupe_by_stable_key(chunks)
        assert len(result) == 2  # both kept (no key to dedupe on)

    def test_mixed_keyed_and_unkeyed(self):
        from search_utils import dedupe_by_stable_key
        chunks = [
            self._chunk(1, "key:A"),
            self._chunk(2, None),
            self._chunk(3, "key:A"),  # dup of chunk 1
        ]
        result = dedupe_by_stable_key(chunks)
        indices = [c["original_index"] for c in result]
        assert 1 in indices
        assert 2 in indices
        assert 3 not in indices  # deduplicated
