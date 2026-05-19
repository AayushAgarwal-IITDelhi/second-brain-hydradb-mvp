"""Tests for query_rewriter.py — person/channel inference from questions."""

import pytest


def _rewrite(question, explicit_channel=None, explicit_user=None):
    from query_rewriter import rewrite_query
    return rewrite_query(
        question,
        explicit_channel=explicit_channel,
        explicit_user=explicit_user,
    )


# ── Empty / trivial input ──────────────────────────────────────────────────
class TestEmptyInput:
    def test_empty_question_returns_no_inference(self):
        r = _rewrite("")
        assert r["inferred_person"] is None
        assert r["inferred_channel"] is None

    def test_none_handled_via_empty_guard(self):
        r = _rewrite(None)
        # Should return the empty result structure without crashing
        assert r["inferred_person"] is None

    def test_whitespace_only_returns_no_inference(self):
        r = _rewrite("   ")
        assert r["inferred_person"] is None
        assert r["inferred_channel"] is None


# ── Strong person patterns ─────────────────────────────────────────────────
class TestStrongPersonInference:
    @pytest.mark.parametrize("question,expected_name", [
        ("What did Alice say about the roadmap?", "Alice"),
        ("did Bob mention the API changes?", "Bob"),
        ("messages from Praveer Nema last week", "Praveer Nema"),
        ("according to Charlie the meeting is cancelled", "Charlie"),
        ("Alice said the deadline is Friday", "Alice"),
        ("what was Diana saying about the sprint?", "Diana"),
        ("written by Eve in the general channel", "Eve"),
    ])
    def test_strong_person_patterns(self, question, expected_name):
        r = _rewrite(question)
        assert r["inferred_person"] == expected_name, (
            f"Expected {expected_name!r} from {question!r}, got {r['inferred_person']!r}"
        )
        assert r["person_confidence"] == "strong"

    def test_strong_inference_appears_in_biases(self):
        r = _rewrite("What did Alice say about the roadmap?")
        assert "person:strong" in r["retrieval_biases_applied"]


# ── Weak person patterns ───────────────────────────────────────────────────
class TestWeakPersonInference:
    @pytest.mark.parametrize("question", [
        "What is Alice's opinion on this?",
        "What is Bob's view on the migration?",
    ])
    def test_weak_person_patterns(self, question):
        r = _rewrite(question)
        assert r["inferred_person"] is not None
        assert r["person_confidence"] == "weak"

    def test_weak_inference_appears_in_biases(self):
        r = _rewrite("What is Alice's take on the proposal?")
        assert "person:weak" in r["retrieval_biases_applied"]


# ── Person blocklist ───────────────────────────────────────────────────────
class TestPersonBlocklist:
    @pytest.mark.parametrize("question", [
        "What did the team say about it?",
        "What did Slack say in the announcement?",
        "Did Monday get mentioned?",
        "What did January bring?",
        "What did anyone say about the deadline?",
    ])
    def test_blocklist_prevents_false_positives(self, question):
        r = _rewrite(question)
        assert r["inferred_person"] is None, (
            f"Got unexpected person={r['inferred_person']!r} from {question!r}"
        )

    def test_product_name_not_inferred_as_person(self):
        r = _rewrite("What did OpenAI say about GPT?")
        assert r["inferred_person"] is None

    def test_generic_role_not_inferred_as_person(self):
        r = _rewrite("What did the engineer say about this?")
        # "engineer" is in the blocklist; no person inference
        assert r["inferred_person"] is None


# ── Strong channel patterns ────────────────────────────────────────────────
class TestStrongChannelInference:
    @pytest.mark.parametrize("question,expected_channel", [
        ("What happened in #product?", "product"),
        ("What happened in #all-second-brain last week?", "all-second-brain"),
        ("discussions in engineering yesterday", "engineering"),
        ("the sales channel this week", "sales"),
        ("what happened in design?", "design"),
    ])
    def test_strong_channel_patterns(self, question, expected_channel):
        r = _rewrite(question)
        assert r["inferred_channel"] == expected_channel, (
            f"Expected {expected_channel!r} from {question!r}, got {r['inferred_channel']!r}"
        )
        assert r["channel_confidence"] == "strong"

    def test_hash_prefix_stripped_from_channel(self):
        r = _rewrite("What happened in #engineering?")
        assert r["inferred_channel"] == "engineering"  # no '#'


# ── Channel blocklist ──────────────────────────────────────────────────────
class TestChannelBlocklist:
    @pytest.mark.parametrize("question", [
        "What happened in the meeting?",
        "What happened in the discussion?",
        "What happened in a thread?",
        "discussed in last week",
    ])
    def test_blocklist_prevents_false_channel_positives(self, question):
        r = _rewrite(question)
        assert r["inferred_channel"] is None, (
            f"Got unexpected channel={r['inferred_channel']!r} from {question!r}"
        )


# ── Explicit overrides suppress inference ─────────────────────────────────
class TestExplicitOverrides:
    def test_explicit_user_suppresses_person_inference(self):
        r = _rewrite(
            "What did Alice say?",
            explicit_user="Bob",  # caller already set user=Bob
        )
        # Person inference should be suppressed; no inferred_person
        assert r["inferred_person"] is None

    def test_explicit_channel_suppresses_channel_inference(self):
        r = _rewrite(
            "What happened in #product?",
            explicit_channel="general",
        )
        assert r["inferred_channel"] is None

    def test_explicit_user_still_allows_channel_inference(self):
        r = _rewrite(
            "What happened in #product?",
            explicit_user="Alice",
        )
        # Channel inference is not suppressed
        assert r["inferred_channel"] == "product"


# ── Return structure ───────────────────────────────────────────────────────
class TestReturnStructure:
    def test_always_returns_required_keys(self):
        required = {
            "inferred_person", "inferred_channel",
            "person_confidence", "channel_confidence",
            "retrieval_biases_applied",
        }
        r = _rewrite("what did Alice say in #sales?")
        assert required.issubset(r.keys())

    def test_biases_applied_is_list(self):
        r = _rewrite("hello world")
        assert isinstance(r["retrieval_biases_applied"], list)

    def test_both_person_and_channel_detected(self):
        r = _rewrite("What did Alice say in #product?")
        assert r["inferred_person"] == "Alice"
        assert r["inferred_channel"] == "product"
        assert len(r["retrieval_biases_applied"]) == 2

    def test_no_inference_empty_biases(self):
        r = _rewrite("what is the latest news?")
        assert r["retrieval_biases_applied"] == []
        assert r["inferred_person"] is None
        assert r["inferred_channel"] is None
