"""
Natural-language date phrase parsing for /api/query and /api/query/stream.

Public entry point:
    parse_date_query(phrase) -> {
        "start_timestamp": float | None,
        "end_timestamp":   float | None,
        "matched":         bool,
        "note":            str,        # short description of how we parsed
        "phrase":          str,        # echo of the input (trimmed)
    }

We try fast pre-canned phrases first (today, yesterday, this week, last week,
this month, last month, last N days/weeks/months, after/before <something>,
"from X to Y"). For anything else we fall back to `dateparser`, which handles
locale-aware phrases like "May 10" or "two days ago".

Returning `matched: False` is NOT an error — the caller treats it as "no
filter applied" and surfaces the note in the debug payload.
"""

import calendar
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

# dateparser is optional at import time so the module is still importable
# without it; we just degrade to the pre-canned phrases.
try:
    import dateparser
except Exception:  # noqa: BLE001
    dateparser = None  # type: ignore[assignment]


SECONDS_PER_DAY = 86400


# ---------------------------------------------------------------------- #
# Time helpers
# ---------------------------------------------------------------------- #
def _now() -> datetime:
    """Indirected so tests can monkeypatch."""
    return datetime.now()


def _start_of_day(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _end_of_day(d: datetime) -> datetime:
    return d.replace(hour=23, minute=59, second=59, microsecond=999_000)


def _to_unix(d: datetime) -> float:
    return d.timestamp()


def _start_of_week(d: datetime) -> datetime:
    """Monday 00:00:00 of the week containing d."""
    monday = _start_of_day(d) - timedelta(days=d.weekday())
    return monday


def _start_of_month(d: datetime) -> datetime:
    return _start_of_day(d.replace(day=1))


def _end_of_month(d: datetime) -> datetime:
    last_day = calendar.monthrange(d.year, d.month)[1]
    return _end_of_day(d.replace(day=last_day))


# ---------------------------------------------------------------------- #
# Pre-canned phrase matchers — fast, predictable, no external dependency
# ---------------------------------------------------------------------- #
_LAST_N_DAYS_RE = re.compile(r"^\s*(?:in\s+the\s+)?last\s+(\d+)\s+days?\s*$", re.IGNORECASE)
_LAST_N_WEEKS_RE = re.compile(r"^\s*(?:in\s+the\s+)?last\s+(\d+)\s+weeks?\s*$", re.IGNORECASE)
_LAST_N_MONTHS_RE = re.compile(r"^\s*(?:in\s+the\s+)?last\s+(\d+)\s+months?\s*$", re.IGNORECASE)
_PAST_N_DAYS_RE = re.compile(r"^\s*past\s+(\d+)\s+days?\s*$", re.IGNORECASE)
_AFTER_RE = re.compile(r"^\s*(?:after|since|from)\s+(.+?)\s*$", re.IGNORECASE)
_BEFORE_RE = re.compile(r"^\s*(?:before|until|up\s+to)\s+(.+?)\s*$", re.IGNORECASE)
_FROM_TO_RE = re.compile(
    r"^\s*from\s+(.+?)\s+(?:to|through|until|-)\s+(.+?)\s*$",
    re.IGNORECASE,
)


def _try_canned(phrase: str, now: datetime) -> Optional[Tuple[float, float, str]]:
    """
    Return (start_unix, end_unix, note) for any phrase we recognize directly,
    or None to let dateparser try.
    """
    p = phrase.strip().lower()
    if not p:
        return None

    if p in ("today",):
        return (_to_unix(_start_of_day(now)), _to_unix(_end_of_day(now)), "today")

    if p in ("yesterday",):
        y = now - timedelta(days=1)
        return (_to_unix(_start_of_day(y)), _to_unix(_end_of_day(y)), "yesterday")

    if p in ("this week",):
        start = _start_of_week(now)
        return (_to_unix(start), _to_unix(_end_of_day(now)), "this week")

    if p in ("last week",):
        start = _start_of_week(now) - timedelta(days=7)
        end = _end_of_day(start + timedelta(days=6))
        return (_to_unix(start), _to_unix(end), "last week")

    if p in ("this month",):
        return (_to_unix(_start_of_month(now)), _to_unix(_end_of_day(now)), "this month")

    if p in ("last month",):
        last_day_prev_month = _start_of_month(now) - timedelta(days=1)
        return (
            _to_unix(_start_of_month(last_day_prev_month)),
            _to_unix(_end_of_month(last_day_prev_month)),
            "last month",
        )

    # "last 7 days" / "past 30 days" etc.
    for regex, label in (
        (_LAST_N_DAYS_RE, "last {n} days"),
        (_PAST_N_DAYS_RE, "past {n} days"),
    ):
        m = regex.match(p)
        if m:
            n = max(1, int(m.group(1)))
            start = _start_of_day(now - timedelta(days=n - 1))
            return (_to_unix(start), _to_unix(_end_of_day(now)), label.format(n=n))

    m = _LAST_N_WEEKS_RE.match(p)
    if m:
        n = max(1, int(m.group(1)))
        start = _start_of_week(now) - timedelta(days=7 * n)
        # End at the last Sunday before this week's Monday.
        end = _end_of_day(_start_of_week(now) - timedelta(days=1))
        return (_to_unix(start), _to_unix(end), f"last {n} weeks")

    m = _LAST_N_MONTHS_RE.match(p)
    if m:
        n = max(1, int(m.group(1)))
        # End: last day of the previous month.
        end_anchor = _start_of_month(now) - timedelta(days=1)
        end = _to_unix(_end_of_month(end_anchor))
        # Start: first day of the month that is (n-1) months before end_anchor.
        y, mo = end_anchor.year, end_anchor.month
        for _ in range(n - 1):
            mo -= 1
            if mo == 0:
                mo = 12
                y -= 1
        start_anchor = datetime(y, mo, 1)
        return (_to_unix(_start_of_day(start_anchor)), end, f"last {n} months")

    return None


def _parse_with_dateparser(
    phrase: str,
    now: datetime,
    prefer_past: bool = False,
) -> Optional[datetime]:
    """
    Wrap dateparser; returns None if it's unavailable or fails.

    `prefer_past=True` is useful for bare-date queries like "May 10" where
    the user almost always means the most recent one (last month's
    meeting, not next month's). `prefer_past=False` (the default) is
    right for "after X" / "before X" / "from X to Y" where the user
    means the nearest such date in either direction — usually current
    year — so dateparser picks the natural reading instead of jumping
    back a full year.
    """
    if dateparser is None:
        return None
    settings: Dict[str, Any] = {
        "RELATIVE_BASE": now,
        "RETURN_AS_TIMEZONE_AWARE": False,
    }
    if prefer_past:
        settings["PREFER_DATES_FROM"] = "past"
    try:
        return dateparser.parse(phrase, settings=settings)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------- #
# Public API
# ---------------------------------------------------------------------- #
def parse_date_query(phrase: Optional[str]) -> Dict[str, Any]:
    """
    Parse a natural date phrase. Always returns a dict — never raises.

    Returns:
        {
            "start_timestamp": float | None,
            "end_timestamp":   float | None,
            "matched":         bool,
            "note":            str,
            "phrase":          str,
        }
    """
    raw = (phrase or "").strip()
    result = {
        "start_timestamp": None,
        "end_timestamp": None,
        "matched": False,
        "note": "no date_query provided" if not raw else "",
        "phrase": raw,
    }
    if not raw:
        return result

    now = _now()

    # 1. Pre-canned phrases.
    canned = _try_canned(raw, now)
    if canned is not None:
        start, end, note = canned
        result.update(
            {
                "start_timestamp": start,
                "end_timestamp": end,
                "matched": True,
                "note": f"matched canned phrase: {note}",
            }
        )
        return result

    # 2. "from X to Y" -> needs dateparser to handle X and Y individually.
    m = _FROM_TO_RE.match(raw)
    if m:
        left, right = m.group(1), m.group(2)
        left_dt = _parse_with_dateparser(left, now)
        right_dt = _parse_with_dateparser(right, now)
        if left_dt and right_dt:
            if left_dt > right_dt:
                left_dt, right_dt = right_dt, left_dt
            result.update(
                {
                    "start_timestamp": _to_unix(_start_of_day(left_dt)),
                    "end_timestamp": _to_unix(_end_of_day(right_dt)),
                    "matched": True,
                    "note": f"parsed 'from {left} to {right}'",
                }
            )
            return result

    # 3. "after X" / "before X" — open-ended ranges.
    m = _AFTER_RE.match(raw)
    if m:
        dt = _parse_with_dateparser(m.group(1), now)
        if dt:
            result.update(
                {
                    "start_timestamp": _to_unix(_start_of_day(dt)),
                    "end_timestamp": None,
                    "matched": True,
                    "note": f"parsed 'after {m.group(1)}'",
                }
            )
            return result

    m = _BEFORE_RE.match(raw)
    if m:
        dt = _parse_with_dateparser(m.group(1), now)
        if dt:
            result.update(
                {
                    "start_timestamp": None,
                    "end_timestamp": _to_unix(_end_of_day(dt)),
                    "matched": True,
                    "note": f"parsed 'before {m.group(1)}'",
                }
            )
            return result

    # 4. Generic dateparser fallback — treat as a single day. Use the
    # past-preference here because a bare date like "May 10" almost
    # always refers to the most recent one (e.g. "show me May 10's
    # discussion" said in late May means this year, but said in early
    # January means last year — past-pref handles both).
    dt = _parse_with_dateparser(raw, now, prefer_past=True)
    if dt:
        result.update(
            {
                "start_timestamp": _to_unix(_start_of_day(dt)),
                "end_timestamp": _to_unix(_end_of_day(dt)),
                "matched": True,
                "note": f"parsed as single day: {dt.date().isoformat()}",
            }
        )
        return result

    result["note"] = (
        "could not parse date phrase; try 'today', 'yesterday', 'last week', "
        "'last 7 days', 'after May 10', 'from May 1 to May 7', or a "
        "specific date like 'May 12 2026'."
    )
    return result
