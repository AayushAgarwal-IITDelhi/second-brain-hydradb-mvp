"""
Heuristic query rewriter.

Detects likely person + channel references in a user's question and
returns them, with a confidence label ("strong" or "weak") that drives
how the retrieval layer reacts:

    strong  -> apply as a hard metadata filter (drop non-matching chunks)
    weak    -> apply as a ranking bias only (matching chunks rank higher)

Everything here is pure-Python, regex-driven, and defensive about
false positives. We don't have an NER model in this stack, so the
heuristics lean towards "infer only when the phrasing makes it obvious"
rather than "guess from any capitalized word".

Public entry point:

    rewrite_query(question, *, explicit_channel=None, explicit_user=None)
      -> {
        "inferred_person":             str | None,
        "inferred_channel":            str | None,
        "person_confidence":           "strong" | "weak" | None,
        "channel_confidence":          "strong" | "weak" | None,
        "retrieval_biases_applied":    list[str],   # debug labels
      }

The caller is expected to:
  - skip inference for any field the user already filled in explicitly
  - combine strong inference into filters before HydraDB recall
  - pass weak inference + filters down to the reranker

This module is intentionally separate from search_utils.py so the
"infer from text" logic stays distinct from "rerank chunks given
already-resolved terms".
"""

import re
from typing import Any, Dict, Optional, Tuple


# ---------------------------------------------------------------------- #
# Tokens we never want to surface as a "person"
# ---------------------------------------------------------------------- #
# Capitalized words that show up commonly in queries but are not people.
# We compare case-insensitively, so list lowercase entries.
_PERSON_BLOCKLIST = {
    # Product / tech terms that often appear Title-Cased
    "slack", "hydradb", "openai", "openrouter", "groq", "claude",
    "second", "brain", "api", "llm", "sql", "url", "json",
    # Generic role nouns
    "team", "engineer", "engineering", "manager", "designer",
    "customer", "customers", "users", "user", "people",
    # Time / calendar tokens
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "today", "yesterday", "tomorrow",
    "q1", "q2", "q3", "q4",
    # Question / determiner words that can get captured by `did X` patterns
    "anyone", "someone", "everybody", "anybody", "nobody", "everyone",
    "that", "this", "those", "these", "the",
}

# Lowercase tokens that look like channel names — we list a few common
# ones plus structural words ("general") so weak channel detection
# doesn't mistake "product" for a person. The BLOCKLIST above already
# covers the person-vs-channel disambiguation for these.
_COMMON_CHANNEL_HINTS = {
    "general", "random", "product", "engineering", "design",
    "sales", "marketing", "support", "ops", "infra", "platform",
    "backend", "frontend", "data", "growth", "hr",
}
# Add channel hints to the person blocklist too, so a phrase like
# "what did product say" doesn't infer person="Product".
_PERSON_BLOCKLIST.update(_COMMON_CHANNEL_HINTS)


# ---------------------------------------------------------------------- #
# Regex helpers
# ---------------------------------------------------------------------- #
# A "name token" is a Title-Case word, optionally followed by another
# Title-Case word (e.g. "Praveer Nema"). We allow apostrophes and
# hyphens inside names ("O'Brien", "Smith-Jones"). Single-word match
# is the common case.
_NAME = r"([A-Z][a-zA-Z'\-]{1,30}(?:\s+[A-Z][a-zA-Z'\-]{1,30})?)"

# A "channel token" is a slug: lowercase letters/digits/hyphens, 2-60 chars.
# We accept either #-prefixed or bare. We capture without the # so the
# downstream filter sees plain channel names.
_CHANNEL = r"#?([a-z][a-z0-9\-]{1,60})"

# ---------- PERSON: strong patterns ----------
_PERSON_STRONG_PATTERNS = [
    # "what did X say/mention/discuss/think/decide"
    re.compile(rf"\bwhat\s+did\s+{_NAME}\s+(?:say|said|mention|discuss|think|decide|propose|comment|share|post|write)\b", re.IGNORECASE),
    # "did X say/mention/..."
    re.compile(rf"\bdid\s+{_NAME}\s+(?:say|mention|discuss|propose|comment|share|post|write)\b", re.IGNORECASE),
    # "messages from X" / "message from X" / "post from X"
    re.compile(rf"\b(?:messages?|posts?|comments?|notes?)\s+(?:from|by)\s+{_NAME}\b", re.IGNORECASE),
    # "according to X" / "per X"
    re.compile(rf"\baccording\s+to\s+{_NAME}\b", re.IGNORECASE),
    # "X said" / "X mentioned" / etc. as the subject of a content verb
    re.compile(rf"\b{_NAME}\s+(?:said|mentioned|discussed|proposed|commented|wrote|posted|noted|shared)\b"),
    # "what was X saying/discussing"
    re.compile(rf"\bwhat\s+was\s+{_NAME}\s+(?:saying|discussing|talking|mentioning)\b", re.IGNORECASE),
    # "by X" right after a content noun
    re.compile(rf"\b(?:said|mentioned|discussed|written|posted|shared)\s+by\s+{_NAME}\b", re.IGNORECASE),
]

# ---------- PERSON: weak patterns ----------
# A bare possessive or addressed-to form that often-but-not-always names
# a person. Used only as a ranking bias.
_PERSON_WEAK_PATTERNS = [
    # "X's view/take/opinion/idea/comment/feedback/...":
    re.compile(rf"\b{_NAME}'s\s+(?:view|take|opinion|idea|comment|feedback|proposal|update|message|post|note)\b"),
    # "to X" / "for X" right after a content verb — common in "what did we tell Rahul"
    re.compile(rf"\b(?:tell|told|ask|asked|reply|replied|respond|responded)\s+(?:to\s+)?{_NAME}\b", re.IGNORECASE),
]

# ---------- CHANNEL: strong patterns ----------
_CHANNEL_STRONG_PATTERNS = [
    # "#sales" or "#all-second-brain" — explicit hash is high-signal
    re.compile(r"#([a-z][a-z0-9\-]{1,60})", re.IGNORECASE),
    # "in <channel>" / "from <channel>" / "to <channel>"
    re.compile(rf"\bin\s+{_CHANNEL}\b", re.IGNORECASE),
    re.compile(rf"\bfrom\s+{_CHANNEL}\s+(?:channel|chat)\b", re.IGNORECASE),
    re.compile(rf"\bposted\s+(?:to|in)\s+{_CHANNEL}\b", re.IGNORECASE),
    # "what happened in <channel>" — strong: very specific phrasing
    re.compile(rf"\bwhat\s+happened\s+in\s+{_CHANNEL}\b", re.IGNORECASE),
    # "discussions in <channel>" / "chat in <channel>"
    re.compile(rf"\b(?:discussions?|chats?|conversations?)\s+in\s+{_CHANNEL}\b", re.IGNORECASE),
    # "<channel> channel" (e.g. "the product channel")
    re.compile(rf"\bthe\s+{_CHANNEL}\s+channel\b", re.IGNORECASE),
]

# ---------- CHANNEL: weak patterns ----------
# (None right now — the strong list already covers the common phrasings.
# We keep the structure so a future contributor can add weak cases here
# without restructuring the resolver below.)
_CHANNEL_WEAK_PATTERNS: list = []


# Words that should NOT be treated as channel names even when they
# follow "in/from/to". These are stopwords or time anchors that look
# slug-shaped after lowercasing.
_CHANNEL_BLOCKLIST = {
    "the", "a", "an", "any", "some", "this", "that", "those", "these",
    "general",   # too generic; if it's a real channel, the user will say "#general"
    "meeting", "call", "discussion", "thread", "channel", "chat",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
    "today", "yesterday", "tomorrow",
    "last", "next", "this",
    "morning", "afternoon", "evening",
    "week", "month", "year", "quarter",
}


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _is_blocked_person(name: str) -> bool:
    """Reject names that are common false-positives."""
    if not name:
        return True
    # If ANY word of a multi-word name is blocked, drop the whole thing.
    # (e.g. "Slack Team" -> blocked.)
    for piece in name.split():
        if piece.lower() in _PERSON_BLOCKLIST:
            return True
    return False


def _is_blocked_channel(slug: str) -> bool:
    if not slug:
        return True
    return slug.lower() in _CHANNEL_BLOCKLIST


def _normalize_channel(slug: str) -> str:
    """Channels are lowercased — Slack channel names are always lowercase."""
    return slug.strip().lower()


def _find_first_match(patterns, text: str) -> Optional[str]:
    """Return the first captured group across a list of patterns."""
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------- #
# Public API
# ---------------------------------------------------------------------- #
def rewrite_query(
    question: str,
    *,
    explicit_channel: Optional[str] = None,
    explicit_user: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Inspect `question` and return any inferred person/channel filters
    plus confidence labels.

    Inference is skipped for any axis the caller already specified
    explicitly (`explicit_channel`, `explicit_user`) — explicit always
    wins. We still emit the *other* axis if it was inferred.
    """
    result: Dict[str, Any] = {
        "inferred_person":          None,
        "inferred_channel":         None,
        "person_confidence":        None,
        "channel_confidence":       None,
        "retrieval_biases_applied": [],
    }
    if not question or not question.strip():
        return result

    text = question

    # ---------- person ----------
    if not (explicit_user and explicit_user.strip()):
        person, confidence = _detect_person(text)
        if person:
            result["inferred_person"] = person
            result["person_confidence"] = confidence
            result["retrieval_biases_applied"].append(
                f"person:{confidence}"
            )

    # ---------- channel ----------
    if not (explicit_channel and explicit_channel.strip()):
        channel, confidence = _detect_channel(text)
        if channel:
            result["inferred_channel"] = channel
            result["channel_confidence"] = confidence
            result["retrieval_biases_applied"].append(
                f"channel:{confidence}"
            )

    return result


def _detect_person(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (name, confidence) or (None, None)."""
    name = _find_first_match(_PERSON_STRONG_PATTERNS, text)
    if name and not _is_blocked_person(name):
        return name.strip(), "strong"

    name = _find_first_match(_PERSON_WEAK_PATTERNS, text)
    if name and not _is_blocked_person(name):
        return name.strip(), "weak"

    return None, None


def _detect_channel(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (channel_slug, confidence) or (None, None)."""
    # Strong: explicit #hash beats everything else.
    hash_match = re.search(r"#([a-z][a-z0-9\-]{1,60})", text, re.IGNORECASE)
    if hash_match:
        slug = _normalize_channel(hash_match.group(1))
        if not _is_blocked_channel(slug):
            return slug, "strong"

    # Strong: the other strong patterns.
    for pat in _CHANNEL_STRONG_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        slug = _normalize_channel(m.group(1))
        if _is_blocked_channel(slug):
            continue
        return slug, "strong"

    # Weak: future expansion point.
    for pat in _CHANNEL_WEAK_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        slug = _normalize_channel(m.group(1))
        if _is_blocked_channel(slug):
            continue
        return slug, "weak"

    return None, None