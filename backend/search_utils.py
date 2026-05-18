"""
Keyword extraction + chunk ranking utilities.

Used by recall.py to support:
  - mode="exact":  prefer chunks that match query terms verbatim; if no
                   exact matches exist, fall back to semantic order but
                   report exact_matches_found=0.
  - mode="hybrid": combine semantic order with an exact-match bonus.

All functions here are pure and side-effect-free so they're trivially
unit-testable.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# Words too common to be useful as exact-match terms. Short list — we don't
# need NLP-grade stopword removal, just enough to stop noise like "the" or
# "what" from dominating the ranking.
STOPWORDS: Set[str] = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "did", "for",
    "from", "had", "has", "have", "how", "i", "if", "in", "into", "is",
    "it", "its", "just", "me", "my", "no", "not", "of", "on", "or", "our",
    "out", "should", "so", "some", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "to", "too", "us", "was",
    "we", "were", "what", "when", "where", "which", "who", "whom", "why",
    "will", "with", "would", "you", "your", "about", "any", "been", "but",
    "can", "could", "did", "does", "doing", "done", "go", "going",
}

# Tokens are alphanumeric + underscore + hyphen.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-]*")
# Phrases enclosed in double quotes: '"exact phrase"' -> "exact phrase".
_QUOTED_PHRASE_RE = re.compile(r'"([^"]+)"')


def extract_query_terms(question: str) -> List[str]:
    """
    Pull keyword terms (and quoted phrases) out of a question.

    Rules:
      - Quoted phrases are extracted whole.
      - Outside quotes, alphanumeric tokens >= 2 chars are kept, lowercased.
      - English stopwords are dropped.
      - Order is preserved; duplicates are removed (keeping first occurrence).
    """
    if not question:
        return []

    terms: List[str] = []
    seen: Set[str] = set()

    # 1. Quoted phrases first.
    remainder = question
    for match in _QUOTED_PHRASE_RE.finditer(question):
        phrase = match.group(1).strip().lower()
        if phrase and phrase not in seen:
            terms.append(phrase)
            seen.add(phrase)
    # Strip the quoted portions out of remainder so they aren't re-tokenized.
    remainder = _QUOTED_PHRASE_RE.sub(" ", remainder)

    # 2. Bare tokens.
    for tok_match in _TOKEN_RE.finditer(remainder):
        tok = tok_match.group(0).lower()
        if len(tok) < 2 or tok in STOPWORDS:
            continue
        if tok in seen:
            continue
        terms.append(tok)
        seen.add(tok)

    return terms


def count_keyword_hits(text: str, terms: Iterable[str]) -> int:
    """
    Count how many distinct query terms appear in the text.

    - Quoted phrases (multi-word terms) are looked up as a substring.
    - Single-word terms are matched as whole words (so "ai" won't match
      "again").

    Returns a per-term hit count: each matching term contributes 1, even if
    it appears multiple times. We want diversity of matches, not raw freq.
    """
    if not text:
        return 0
    hay = text.lower()
    hits = 0
    for term in terms:
        if not term:
            continue
        if " " in term:
            # multi-word / quoted phrase -> substring match
            if term in hay:
                hits += 1
        else:
            # single token -> word-boundary match
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])"
            if re.search(pattern, hay):
                hits += 1
    return hits


def _ts_to_float(value: Any) -> Optional[float]:
    """Slack ts string or numeric -> float seconds, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _metadata_bias_score(
    source_card: Dict[str, Any],
    bias: Optional[Dict[str, str]],
) -> int:
    """
    Count how many fields of `bias` (typically {"channel": ..., "user": ...})
    match the chunk's source card. Comparisons are case-insensitive on
    string values; missing card fields don't penalize, they just don't
    contribute.

    Returns an integer 0..len(bias) — used by the sort key in rerank_chunks
    to push matching chunks up.
    """
    if not bias:
        return 0
    score = 0
    for key, value in bias.items():
        if not value:
            continue
        card_value = source_card.get(key)
        if isinstance(card_value, str) and isinstance(value, str):
            if card_value.lower() == value.lower():
                score += 1
    return score


def rerank_chunks(
    chunks_with_meta: List[Dict[str, Any]],
    terms: List[str],
    mode: str,
    top_k: int,
    metadata_bias: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Rerank a list of `{text, source_card, original_index, timestamp_float}`
    dicts according to `mode`, with an optional `metadata_bias`.

    Modes:
      - "exact":  Keep chunks with >=1 keyword hit, sorted by hit-count then
                  by metadata bias, then timestamp (newest first). If no
                  chunk has any hit, fall back to semantic order biased by
                  metadata.
      - "hybrid": Score each chunk = (hits*100) + (bias*50) + (newer_score).
                  Even chunks with 0 hits stay, deprioritized.
      - anything else (incl. "default"): preserve semantic order, but push
                  chunks matching `metadata_bias` ahead of the rest. This
                  lets person/channel inference improve results in modes
                  that don't otherwise rerank.

    `metadata_bias` looks like `{"channel": "product", "user": "rahul"}`.
    Each matching key adds to a per-chunk bias score. Comparisons are
    case-insensitive; non-string card values are ignored.

    Returns (ranked_chunks, exact_matches_found).
      `ranked_chunks` is capped at `top_k`. `exact_matches_found` is the
      count of chunks with >=1 keyword hit (whether kept or not).
    """
    if not chunks_with_meta:
        return [], 0

    # Annotate every chunk with its hit count + metadata bias score so the
    # mode-specific sort keys below can use both signals.
    for chunk in chunks_with_meta:
        chunk["_hits"] = count_keyword_hits(chunk.get("text", ""), terms)
        chunk["_bias"] = _metadata_bias_score(
            chunk.get("source_card", {}), metadata_bias,
        )
    matched_count = sum(1 for c in chunks_with_meta if c["_hits"] > 0)

    if mode == "exact":
        matched = [c for c in chunks_with_meta if c["_hits"] > 0]
        if matched:
            # Sort key: (hits desc, bias desc, newer first, stable index).
            matched.sort(
                key=lambda c: (
                    -c["_hits"],
                    -c["_bias"],
                    -(c.get("timestamp_float") or 0.0),
                    c["original_index"],
                ),
            )
            return matched[:top_k], matched_count
        # No exact matches: fall back to semantic order but still let
        # the metadata bias surface relevant chunks above unrelated ones.
        ranked = sorted(
            chunks_with_meta,
            key=lambda c: (
                -c["_bias"],
                c["original_index"],
            ),
        )
        return ranked[:top_k], 0

    if mode == "hybrid":
        max_ts = max(
            (c.get("timestamp_float") or 0.0) for c in chunks_with_meta
        ) or 1.0
        ranked = sorted(
            chunks_with_meta,
            key=lambda c: -(
                c["_hits"] * 100
                + c["_bias"] * 50
                + (c.get("timestamp_float") or 0.0) / max_ts
            ),
        )
        return ranked[:top_k], matched_count

    # Default mode + summary/decisions/etc. — preserve semantic order, but
    # still let metadata bias push matching chunks up. When bias is zero
    # for everything (no inference), the sort collapses to original_index
    # which is identical to the input order.
    ranked = sorted(
        chunks_with_meta,
        key=lambda c: (
            -c["_bias"],
            c["original_index"],
        ),
    )
    return ranked[:top_k], matched_count


def dedupe_by_stable_key(chunks_with_meta: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Drop later chunks whose stable_key was already seen.
    First occurrence wins (best score-rank for the document).
    """
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for chunk in chunks_with_meta:
        key = chunk.get("source_card", {}).get("stable_key")
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(chunk)
    return out