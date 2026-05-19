"""Tests for date_utils.py — natural-language date phrase parsing."""

from datetime import datetime, timedelta

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────
def _parse(phrase):
    from date_utils import parse_date_query
    return parse_date_query(phrase)


def _now_fixed() -> datetime:
    """A fixed 'now' so tests are deterministic regardless of wall time."""
    return datetime(2026, 5, 15, 12, 0, 0)


# ── None / empty input ──────────────────────────────────────────────────────
class TestNullAndEmpty:
    def test_none_returns_no_match(self):
        result = _parse(None)
        assert result["matched"] is False
        assert result["phrase"] == ""

    def test_empty_string_returns_no_match(self):
        result = _parse("")
        assert result["matched"] is False

    def test_whitespace_only_returns_no_match(self):
        result = _parse("   ")
        assert result["matched"] is False


# ── Canned phrases ─────────────────────────────────────────────────────────
class TestCannedPhrases:
    def setup_method(self):
        import date_utils
        date_utils._now = _now_fixed

    def teardown_method(self):
        import date_utils
        date_utils._now = lambda: datetime.now()

    def test_today(self):
        r = _parse("today")
        assert r["matched"] is True
        assert r["start_timestamp"] is not None
        assert r["end_timestamp"] is not None
        # start < end
        assert r["start_timestamp"] < r["end_timestamp"]

    def test_yesterday(self):
        r = _parse("yesterday")
        assert r["matched"] is True
        now = _now_fixed()
        yesterday_start = datetime(now.year, now.month, now.day - 1, 0, 0, 0).timestamp()
        assert abs(r["start_timestamp"] - yesterday_start) < 1

    def test_this_week(self):
        r = _parse("this week")
        assert r["matched"] is True
        # End should be today/now, start should be Monday
        now = _now_fixed()
        assert r["start_timestamp"] <= now.timestamp()
        assert r["end_timestamp"] >= now.timestamp()

    def test_last_week(self):
        r = _parse("last week")
        assert r["matched"] is True
        assert r["start_timestamp"] < r["end_timestamp"]
        now = _now_fixed()
        # Last week entirely in the past
        assert r["end_timestamp"] < now.timestamp()

    def test_this_month(self):
        r = _parse("this month")
        assert r["matched"] is True
        now = _now_fixed()
        assert r["start_timestamp"] <= now.timestamp()

    def test_last_month(self):
        r = _parse("last month")
        assert r["matched"] is True
        now = _now_fixed()
        assert r["end_timestamp"] < now.timestamp()

    def test_last_7_days(self):
        r = _parse("last 7 days")
        assert r["matched"] is True
        now = _now_fixed()
        span = r["end_timestamp"] - r["start_timestamp"]
        assert 6 * 86400 <= span <= 8 * 86400

    def test_last_30_days(self):
        r = _parse("last 30 days")
        assert r["matched"] is True

    def test_past_14_days(self):
        r = _parse("past 14 days")
        assert r["matched"] is True

    def test_last_2_weeks(self):
        r = _parse("last 2 weeks")
        assert r["matched"] is True
        # Span should be approximately 2 weeks
        span_days = (r["end_timestamp"] - r["start_timestamp"]) / 86400
        assert 13 <= span_days <= 16

    def test_last_3_months(self):
        r = _parse("last 3 months")
        assert r["matched"] is True

    @pytest.mark.parametrize("phrase", [
        "today", "yesterday", "this week", "last week",
        "this month", "last month",
    ])
    def test_canned_phrases_return_note(self, phrase):
        r = _parse(phrase)
        assert r["matched"] is True
        assert r["note"] != ""

    @pytest.mark.parametrize("phrase", [
        "today", "yesterday", "this week", "last week",
        "this month", "last month",
    ])
    def test_canned_phrases_echo_phrase(self, phrase):
        r = _parse(phrase)
        assert r["phrase"] == phrase


# ── After / before / from-to ───────────────────────────────────────────────
class TestRangePhrases:
    def setup_method(self):
        import date_utils
        date_utils._now = _now_fixed

    def teardown_method(self):
        import date_utils
        date_utils._now = lambda: datetime.now()

    def test_after_date(self):
        r = _parse("after May 10")
        assert r["matched"] is True
        assert r["start_timestamp"] is not None
        assert r["end_timestamp"] is None

    def test_since_date(self):
        r = _parse("since May 10")
        assert r["matched"] is True
        assert r["start_timestamp"] is not None

    def test_before_date(self):
        r = _parse("before May 10")
        assert r["matched"] is True
        assert r["end_timestamp"] is not None
        assert r["start_timestamp"] is None

    def test_from_to(self):
        r = _parse("from May 1 to May 7")
        assert r["matched"] is True
        assert r["start_timestamp"] is not None
        assert r["end_timestamp"] is not None
        assert r["start_timestamp"] < r["end_timestamp"]

    def test_from_to_reversed_auto_swaps(self):
        """If the user writes 'from May 7 to May 1', we swap to keep start < end."""
        r = _parse("from May 7 to May 1")
        if r["matched"]:
            assert r["start_timestamp"] <= r["end_timestamp"]

    def test_from_through(self):
        r = _parse("from May 1 through May 7")
        # dateparser handles 'through' as 'to' in some locales
        # We accept matched or not-matched — just no crash
        assert isinstance(r["matched"], bool)


# ── Unrecognised phrases ───────────────────────────────────────────────────
class TestUnrecognisedPhrases:
    def test_garbage_returns_not_matched(self):
        r = _parse("purple elephant dancing")
        assert r["matched"] is False

    def test_not_matched_has_helpful_note(self):
        r = _parse("purple elephant dancing")
        assert r["note"] != ""
        assert "today" in r["note"].lower() or "yesterday" in r["note"].lower()

    def test_not_matched_still_echoes_phrase(self):
        r = _parse("blorg fleebity")
        assert r["phrase"] == "blorg fleebity"

    def test_timestamps_are_none_on_no_match(self):
        r = _parse("definitely not a date")
        assert r["start_timestamp"] is None
        assert r["end_timestamp"] is None


# ── Return type consistency ────────────────────────────────────────────────
class TestReturnTypes:
    def test_always_returns_dict(self):
        for phrase in [None, "", "today", "garbage xyz"]:
            r = _parse(phrase)
            assert isinstance(r, dict)

    def test_always_has_required_keys(self):
        required = {"start_timestamp", "end_timestamp", "matched", "note", "phrase"}
        for phrase in [None, "", "today", "last week", "garbage"]:
            r = _parse(phrase)
            assert required.issubset(r.keys()), f"Missing keys for phrase={phrase!r}"

    def test_timestamps_are_floats_when_matched(self):
        r = _parse("today")
        if r["matched"] and r["start_timestamp"] is not None:
            assert isinstance(r["start_timestamp"], float)
        if r["matched"] and r["end_timestamp"] is not None:
            assert isinstance(r["end_timestamp"], float)
