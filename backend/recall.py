"""
Recall + answer orchestration for the Second Brain MVP.

Pipeline:
    user question
        -> HydraDB /recall/full_recall   (semantic retrieval)
        -> extract & number context chunks
        -> cloud LLM with strict grounding prompt
        -> { "answer": ..., "sources": [...], "debug": {...} }

Notes on extraction strategy:
HydraDB's recall response shape varies. We probe an ordered list of
"shallow" text fields first (so we don't accidentally return JSON metadata
when a clean `text` field exists somewhere deeper), and only fall back to a
recursive dict-walk if every documented path comes up empty.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from hydradb_client import HydraDBClient
from llm import generate_grounded_answer
from prompts import INSUFFICIENT_CONTEXT_ANSWER
from ingestion.ingestion_state import IngestionState


# Path to the ingestion state file (written by ingest_slack.py). Loaded once
# per request inside answer_question, never re-read between chunks.
_BACKEND_DIR = Path(__file__).resolve().parent
STATE_PATH = _BACKEND_DIR / "data" / "ingestion_state.json"


def _debug_recall_enabled() -> bool:
    """
    Verbose recall debugging is OFF by default to keep demo logs clean and
    avoid leaking Slack content through stdout. Flip DEBUG_RECALL=true in
    the environment to see the raw HydraDB response and first-chunk preview.
    """
    return os.getenv("DEBUG_RECALL", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


# Where to look for the list of chunks at the top level of the recall response.
CHUNKS_KEYS = ("chunks", "results", "documents", "matches", "items", "data")

# Direct text fields on a chunk object, tried in order. First non-empty wins.
DIRECT_TEXT_KEYS = (
    "text",
    "content",
    "chunk",
    "body",
    "page_content",
    "document",
    "memory",
    "value",
    "data",
    "raw_text",
    "chunk_text",
)

# Dotted paths to try when the direct keys above all miss.
NESTED_TEXT_PATHS = (
    ("payload", "text"),
    ("payload", "content"),
    ("metadata", "text"),
    ("metadata", "content"),
    ("metadata", "chunk_text"),
    ("metadata", "document"),
    ("metadata", "raw_text"),
    ("source", "text"),
    ("source", "content"),
)

# Source-identifier fields on a chunk, tried in order.
DIRECT_SOURCE_KEYS = (
    "source_id",
    "filename",
    "source",
    "doc_id",
    "document_id",
    "id",
    "name",
)
NESTED_SOURCE_PATHS = (
    ("metadata", "filename"),
    ("metadata", "source_id"),
    ("metadata", "source"),
    ("metadata", "channel"),
    ("metadata", "channel_id"),
)

# Score-ish fields.
SCORE_KEYS = ("score", "similarity", "distance", "relevance")

# Field names that look like metadata, not body text. We avoid these when
# falling back to a recursive search.
NON_TEXT_FIELD_NAMES = {
    "id", "doc_id", "document_id", "source_id", "filename", "source",
    "channel", "channel_id", "thread_ts", "ts", "user_id", "user", "url",
    "permalink", "score", "similarity", "distance", "relevance", "type",
    "doc_type", "name", "tenant_id", "sub_tenant_id", "mime_type",
}

MIN_REAL_TEXT_LEN = 20  # below this, a string probably isn't body content


# ---------------------------------------------------------------------- #
# Generic helpers
# ---------------------------------------------------------------------- #
def _get_path(obj: Any, path) -> Any:
    """Walk a tuple of keys into nested dicts. Return None if any step misses."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _coerce_to_text(value: Any) -> str:
    """
    Turn a candidate value into clean text without dropping legitimate content.

    - str  -> the string itself (stripped)
    - list -> if all items look stringy, join with newlines; otherwise ""
    - dict -> "" (callers handle dicts via recursive search)
    - other -> ""
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
        if parts and len(parts) == sum(1 for x in value if isinstance(x, str)):
            return "\n".join(parts)
        return ""
    return ""


def _recursive_find_text(obj: Any, depth: int = 0) -> str:
    """
    Last-resort walk: descend through a dict/list looking for the longest
    plausible body-text string. Skips fields that are obviously metadata.

    Capped at depth 4 to avoid pathological structures.
    """
    if depth > 4:
        return ""

    best = ""

    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in NON_TEXT_FIELD_NAMES:
                continue
            if isinstance(value, str):
                candidate = value.strip()
                if len(candidate) >= MIN_REAL_TEXT_LEN and len(candidate) > len(best):
                    best = candidate
            elif isinstance(value, (dict, list)):
                deeper = _recursive_find_text(value, depth + 1)
                if len(deeper) > len(best):
                    best = deeper

    elif isinstance(obj, list):
        for item in obj:
            deeper = _recursive_find_text(item, depth + 1)
            if len(deeper) > len(best):
                best = deeper

    return best


# ---------------------------------------------------------------------- #
# Chunk-level extractors
# ---------------------------------------------------------------------- #
def _extract_chunks(payload: Dict[str, Any]) -> List[Any]:
    """Pull the chunks list out of whatever shape HydraDB returned."""
    if not isinstance(payload, dict):
        return []

    for key in CHUNKS_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value

    data = payload.get("data")
    if isinstance(data, dict):
        for key in CHUNKS_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def _chunk_text(chunk: Any) -> str:
    """
    Pull readable body text out of a single chunk.

    Order of preference:
      1. The chunk is itself a string.
      2. A direct top-level text field (text/content/chunk/body/...).
         If that field is a dict, recursively search inside it.
      3. A documented nested path (payload.text, metadata.content, etc.).
      4. Last resort: recursive walk of the whole chunk, picking the
         longest non-metadata string.
    """
    if isinstance(chunk, str):
        return chunk.strip()
    if not isinstance(chunk, dict):
        return ""

    # 1. Direct top-level text-like fields.
    for key in DIRECT_TEXT_KEYS:
        if key not in chunk:
            continue
        value = chunk[key]
        text = _coerce_to_text(value)
        if text:
            return text
        # If the value is a dict, dig into it before moving on.
        if isinstance(value, dict):
            deeper = _recursive_find_text(value)
            if deeper:
                return deeper

    # 2. Documented nested paths.
    for path in NESTED_TEXT_PATHS:
        value = _get_path(chunk, path)
        text = _coerce_to_text(value)
        if text:
            return text

    # 3. Recursive fallback across the whole chunk.
    deeper = _recursive_find_text(chunk)
    if deeper:
        return deeper

    return ""


def _chunk_source(chunk: Any, index: int) -> str:
    """Pull a short source identifier; fall back to 'chunk_N'."""
    if not isinstance(chunk, dict):
        return f"chunk_{index}"

    for key in DIRECT_SOURCE_KEYS:
        value = chunk.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for path in NESTED_SOURCE_PATHS:
        value = _get_path(chunk, path)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return f"chunk_{index}"


def _chunk_score(chunk: Any) -> Optional[Any]:
    """Pull a numeric score if available, else None."""
    if not isinstance(chunk, dict):
        return None
    for key in SCORE_KEYS:
        if key in chunk:
            return chunk.get(key)
    meta = chunk.get("metadata")
    if isinstance(meta, dict):
        for key in SCORE_KEYS:
            if key in meta:
                return meta.get(key)
    return None


# ---------------------------------------------------------------------- #
# Debug helpers
# ---------------------------------------------------------------------- #
def _safe_json(obj: Any, limit: int = 8000) -> str:
    """Pretty-print as JSON, truncating to `limit` characters."""
    try:
        text = json.dumps(obj, indent=2, default=str)
    except Exception:
        text = repr(obj)
    if len(text) > limit:
        text = text[:limit] + f"\n... [truncated, {len(text)} chars total]"
    return text


def _first_chunk_preview(chunk: Any, limit: int = 2000) -> str:
    """Compact JSON preview of the first chunk, for the debug payload."""
    return _safe_json(chunk, limit=limit)


# ---------------------------------------------------------------------- #
# Linking recall chunks back to ingestion state
# ---------------------------------------------------------------------- #
# Fields on a chunk that might carry the HydraDB source_id, the stable_key
# we wrote into the markdown, or the filename — any one of these is enough
# to find the rich metadata row in ingestion_state.json.
SOURCE_ID_KEYS = ("source_id", "doc_id", "document_id", "id")
STABLE_KEY_KEYS = ("stable_key", "source_key")
FILENAME_KEYS = ("filename", "file_name", "name")


def _candidate_string(chunk: Dict[str, Any], keys) -> Optional[str]:
    """First non-empty string value found at chunk[k] or chunk['metadata'][k]."""
    for key in keys:
        value = chunk.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    meta = chunk.get("metadata")
    if isinstance(meta, dict):
        for key in keys:
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _build_source_card(
    chunk: Any,
    index: int,
    score: Any,
    state: Optional[IngestionState],
) -> Dict[str, Any]:
    """
    Build the UI-friendly source object for one recall chunk.

    Resolution order against ingestion state:
      1. by source_id     (set by HydraDB on upload)
      2. by stable_key    (written into the markdown header)
      3. by filename      (the .md filename we sent)

    If state lookup fails, fall back to the previous minimal shape:
        {"index", "source", "score"}
    """
    minimal_source = _chunk_source(chunk, index)

    candidate_source_id = (
        _candidate_string(chunk, SOURCE_ID_KEYS) if isinstance(chunk, dict) else None
    )
    candidate_stable_key = (
        _candidate_string(chunk, STABLE_KEY_KEYS) if isinstance(chunk, dict) else None
    )
    candidate_filename = (
        _candidate_string(chunk, FILENAME_KEYS) if isinstance(chunk, dict) else None
    )

    entry: Optional[Dict[str, Any]] = None
    if state is not None:
        if candidate_source_id:
            entry = state.find_by_source_id(candidate_source_id)
        if entry is None and candidate_stable_key:
            entry = state.get(candidate_stable_key)
        if entry is None and candidate_filename:
            entry = state.find_by_filename(candidate_filename)
        # `minimal_source` might itself be the source_id (current behavior)
        # or the filename. Try it as a last resort before giving up.
        if entry is None and minimal_source:
            entry = (
                state.find_by_source_id(minimal_source)
                or state.find_by_filename(minimal_source)
            )

    if entry is None:
        # Graceful fallback: keep the old minimal shape so callers can still
        # render something even if state is missing or doesn't know the doc.
        return {
            "index":  index,
            "source": minimal_source,
            "score":  score,
        }

    # Rich source card backed by ingestion state.
    return {
        "index":         index,
        "source":        entry.get("channel_name") or minimal_source,
        "channel":       entry.get("channel_name"),
        "channel_id":    entry.get("channel_id"),
        "user":          entry.get("user_name"),
        "timestamp":     entry.get("timestamp"),
        "snippet":       entry.get("snippet"),
        "permalink":     entry.get("permalink"),
        "stable_key":    entry.get("stable_key"),
        "document_type": entry.get("document_type"),
        "score":         score,
    }


def _load_state_safely() -> Optional[IngestionState]:
    """Load ingestion state once per request. Return None on any failure."""
    try:
        return IngestionState(STATE_PATH)
    except Exception as e:  # noqa: BLE001 -- we never want recall to crash on this
        print(f"[recall] Could not load ingestion state: {e}")
        return None


# ---------------------------------------------------------------------- #
# Source-list cleaning for the UI
# ---------------------------------------------------------------------- #
# A "rich" source has at least one Slack-derived field. Anything else is a
# minimal fallback (just an opaque HydraDB id / chunk_N placeholder).
RICH_SOURCE_FIELDS = ("channel", "user", "snippet", "permalink", "stable_key")


def _is_rich_source(source: Dict[str, Any]) -> bool:
    """True if the source card carries any UI-useful Slack metadata."""
    return any(source.get(field) for field in RICH_SOURCE_FIELDS)


def _dedupe_key(source: Dict[str, Any]) -> Optional[str]:
    """Identifier used to dedupe sources: stable_key -> permalink -> source."""
    for field in ("stable_key", "permalink", "source"):
        value = source.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _clean_sources_for_ui(
    sources: List[Dict[str, Any]],
    top_k: int,
) -> List[Dict[str, Any]]:
    """
    Trim the sources array for the API response WITHOUT touching the LLM
    context. Rules:
      1. If at least one rich source exists, drop the minimal ones.
      2. Dedupe by stable_key -> permalink -> source (first occurrence wins,
         which keeps the earliest matching index so citations stay aligned).
      3. Cap the result at top_k.
      4. If there are no rich sources at all, fall back to deduped minimal.
    """
    if not sources:
        return []

    rich = [s for s in sources if _is_rich_source(s)]
    pool = rich if rich else sources

    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for source in pool:
        key = _dedupe_key(source)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        # If we have no key to dedupe on, let the source through — we can't
        # safely call it a duplicate of anything.
        deduped.append(source)

    return deduped[:top_k]


# ---------------------------------------------------------------------- #
# Citation hygiene
# ---------------------------------------------------------------------- #
# The LLM emits citation markers that look like:
#     [1]
#     【1】
#     【1†source: slack_all-second-brain_1778775842.md】
#
# Cleaning the sources list can drop indexes (e.g. dedupe collapses [4] into
# [1]). If the answer still references those gone indexes the UI shows
# dangling citations. We strip exactly those markers — and only those.
#
# Pattern explanation:
#   - ASCII form:  '[', optional spaces, digits, optional spaces, ']'
#   - CJK form:    '【', optional spaces, digits, optional spaces,
#                  optional '†<anything but the closing bracket>',
#                  '】'
# Two alternates, two capture groups; whichever matched holds the integer.
_CITATION_PATTERN = re.compile(
    r"\[\s*(\d+)\s*\]"
    r"|"
    r"【\s*(\d+)\s*(?:†[^】]*)?】"
)


def _strip_invalid_citations(answer: str, allowed_indexes: Set[int]) -> str:
    """
    Remove citation markers from `answer` whose index is NOT in
    `allowed_indexes`. Valid citation markers are left exactly as they are.

    Per spec, we do not touch any other character — surrounding whitespace
    or punctuation around a removed marker is left untouched. Some UI
    renderers may show a small extra space; that's fine.
    """
    if not answer or not allowed_indexes:
        # No allowed indexes means everything is invalid -> strip them all.
        # No answer -> nothing to do.
        if not answer:
            return answer

    def _replace(match: re.Match) -> str:
        idx_str = match.group(1) or match.group(2)
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            return match.group(0)  # unparseable -> leave alone (safer)
        return match.group(0) if idx in allowed_indexes else ""

    return _CITATION_PATTERN.sub(_replace, answer)


def _coerce_to_unix_seconds(value: Any) -> Optional[float]:
    """
    Accept either a Slack ts string ('1778775842.876209') or a numeric
    unix timestamp (int or float) and return seconds as a float.
    Returns None if we can't parse.
    """
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


def _source_passes_filters(
    source_card: Dict[str, Any],
    channel: Optional[str],
    user: Optional[str],
    document_type: Optional[str],
    start_unix: Optional[float] = None,
    end_unix: Optional[float] = None,
) -> bool:
    """
    Return True if this source card should be kept given the filters.

    Filters only apply when the source card carries the corresponding
    metadata. If a card was built from a chunk that didn't join to state
    (no `channel` / `user` / `document_type` / `timestamp` fields), we
    let it through for the metadata-style filters — better to over-
    include than to silently drop legitimate matches.

    Date filters are slightly stricter: a card with no parseable
    timestamp is also let through, on the same "don't drop" principle.
    """
    if channel:
        card_channel = source_card.get("channel")
        if isinstance(card_channel, str) and card_channel.lower() != channel.lower():
            return False
    if user:
        card_user = source_card.get("user")
        if isinstance(card_user, str) and card_user.lower() != user.lower():
            return False
    if document_type:
        card_type = source_card.get("document_type")
        if isinstance(card_type, str) and card_type != document_type:
            return False
    if start_unix is not None or end_unix is not None:
        ts_value = _coerce_to_unix_seconds(source_card.get("timestamp"))
        if ts_value is not None:
            if start_unix is not None and ts_value < start_unix:
                return False
            if end_unix is not None and ts_value > end_unix:
                return False
    return True


# ---------------------------------------------------------------------- #
# Reusable building blocks: streaming and non-streaming endpoints share these
# ---------------------------------------------------------------------- #
def prepare_recall_context(
    question: str,
    top_k: int,
    mode: str = "default",
    channel: Optional[str] = None,
    user: Optional[str] = None,
    document_type: Optional[str] = None,
    start_timestamp: Any = None,
    end_timestamp: Any = None,
    metadata_bias: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Run HydraDB recall and build the LLM-ready context + parallel sources.

    The `mode` parameter changes how we rank the chunks BEFORE they're
    handed to the LLM:
      - "exact":  prefer chunks containing the user's keywords verbatim.
                  Fall back to semantic order if no exact matches exist.
      - "hybrid": combine semantic order with a keyword-hit bonus.
      - all others: preserve HydraDB's semantic order.

    `metadata_bias` (e.g. `{"channel": "product", "user": "rahul"}`) is
    forwarded to the reranker. Matching chunks get a ranking boost in
    every mode — useful for weak person/channel inference where we don't
    want to hard-filter but DO want matching chunks to surface first.
    Strong inference should already have been collapsed into the
    `channel` / `user` arguments before this function is called.

    Returns a dict with one of two shapes:

    Success:
        {
            "ready":            True,
            "context_text":     str,                  # numbered context for LLM
            "sources":          List[Dict[str, Any]], # parallel cards (re-numbered)
            "chunks_count":     int,
            "filtered_out":     int,
            "exact_matches":    int,                  # >=1 keyword hit
            "retrieval_mode":   str,                  # what we actually did
            "query_terms":      List[str],
            "fallback_debug":   None,
        }

    No usable context:
        {
            "ready":          False,
            "fallback_debug": Dict[str, Any],
        }
    """
    # Local imports keep recall.py's top-of-file import block stable.
    from search_utils import (
        dedupe_by_stable_key,
        extract_query_terms,
        rerank_chunks,
    )

    hydra = HydraDBClient()
    raw_response = hydra.full_recall(query=question, top_k=top_k)
    chunks = _extract_chunks(raw_response)
    debug_on = _debug_recall_enabled()

    if debug_on:
        print("[recall] ===== raw HydraDB response (truncated to 8000 chars) =====")
        print(_safe_json(raw_response, limit=8000))
        if chunks:
            print("[recall] ===== first chunk =====")
            print(_safe_json(chunks[0], limit=4000))
        else:
            print("[recall] (no chunks returned)")
        print("[recall] ===========================================================")

    state = _load_state_safely()
    start_unix = _coerce_to_unix_seconds(start_timestamp)
    end_unix = _coerce_to_unix_seconds(end_timestamp)
    query_terms = extract_query_terms(question) if mode in ("exact", "hybrid") else []

    # ---- Step 1: gather per-chunk metadata for ranking ----
    # We build a parallel list of {text, source_card, original_index,
    # timestamp_float, hits(_)}. Filters drop chunks before ranking so
    # the rerank operates only on the candidate set the user actually
    # wants.
    chunks_with_meta: List[Dict[str, Any]] = []
    filtered_out = 0
    for original_index, chunk in enumerate(chunks, start=1):
        text = _chunk_text(chunk).strip()
        if not text:
            continue
        score = _chunk_score(chunk)
        # The 'index' we initially put on the card is the HydraDB order.
        # We'll renumber after reranking so the cards line up with the
        # [N] labels the LLM sees.
        source_card = _build_source_card(chunk, original_index, score, state)

        if not _source_passes_filters(
            source_card, channel, user, document_type, start_unix, end_unix,
        ):
            filtered_out += 1
            continue

        chunks_with_meta.append({
            "text": text,
            "source_card": source_card,
            "original_index": original_index,
            "timestamp_float": _coerce_to_unix_seconds(source_card.get("timestamp")),
        })

    # ---- Step 2: dedupe by stable_key BEFORE ranking ----
    # Same Slack message that resurfaces twice in recall shouldn't get
    # twice the ranking boost.
    chunks_with_meta = dedupe_by_stable_key(chunks_with_meta)

    # ---- Step 3: rerank if the mode asks for it; otherwise just cap ----
    ranked, exact_matches_found = rerank_chunks(
        chunks_with_meta, query_terms, mode, top_k,
        metadata_bias=metadata_bias,
    )

    if not ranked:
        first_chunk = chunks[0] if chunks else None
        first_chunk_keys = (
            list(first_chunk.keys()) if isinstance(first_chunk, dict) else None
        )
        debug_payload: Dict[str, Any] = {
            "reason": "no usable text found in HydraDB chunks",
            "raw_response_keys": (
                list(raw_response.keys())
                if isinstance(raw_response, dict) else None
            ),
            "chunks_returned":     len(chunks),
            "chunks_filtered_out": filtered_out,
            "first_chunk_keys":    first_chunk_keys,
            "retrieval_mode":      mode,
            "exact_matches_found": 0,
            "query_terms":         query_terms,
            "top_k":               top_k,
        }
        if debug_on and first_chunk is not None:
            debug_payload["first_chunk_preview"] = _first_chunk_preview(first_chunk)
        return {"ready": False, "fallback_debug": debug_payload}

    # ---- Step 4: renumber 1..N so citations align with surviving cards ----
    context_blocks: List[str] = []
    sources: List[Dict[str, Any]] = []
    for i, chunk in enumerate(ranked, start=1):
        text = chunk["text"]
        card = dict(chunk["source_card"])
        card["index"] = i  # overwrite the original-order index
        context_label = card.get("channel") or card.get("source")
        context_blocks.append(f"[{i}] (source: {context_label})\n{text}")
        sources.append(card)

    return {
        "ready":            True,
        "context_text":     "\n\n".join(context_blocks),
        "sources":          sources,
        "chunks_count":     len(chunks),
        "filtered_out":     filtered_out,
        "exact_matches":    exact_matches_found,
        "retrieval_mode":   mode,
        "query_terms":      query_terms,
        "fallback_debug":   None,
    }


def finalize_answer(
    raw_answer: str,
    sources: List[Dict[str, Any]],
    top_k: int,
) -> Dict[str, Any]:
    """
    Apply source-cleaning + citation-stripping to a raw LLM answer.

    Used by BOTH /api/query and /api/query/stream so the output post-
    processing is identical regardless of how the answer text arrived.
    """
    cleaned_sources = _clean_sources_for_ui(sources, top_k=top_k)
    allowed_indexes: Set[int] = {
        s["index"] for s in cleaned_sources
        if isinstance(s.get("index"), int)
    }
    final_answer = _strip_invalid_citations(raw_answer, allowed_indexes)
    return {
        "answer":          final_answer,
        "cleaned_sources": cleaned_sources,
        "sources_before":  len(sources),
        "sources_after":   len(cleaned_sources),
    }


# ---------------------------------------------------------------------- #
# Public entry point (non-streaming)
# ---------------------------------------------------------------------- #
def answer_question(
    question: str,
    top_k: int = 5,
    mode: str = "default",
    channel: Optional[str] = None,
    user: Optional[str] = None,
    document_type: Optional[str] = None,
    start_timestamp: Any = None,
    end_timestamp: Any = None,
    conversation_history: Optional[List[Any]] = None,
    metadata_bias: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    End-to-end recall + grounded answer.

    Args:
        question:              the natural-language question.
        top_k:                 how many chunks to ask HydraDB for.
        mode:                  "default" | "summary" | "decisions" | "action_items"
                               | "who_said" | "exact" | "hybrid" — selects the
                               system prompt AND the retrieval/ranking strategy.
        channel:               optional channel-name filter (e.g. "general").
        user:                  optional user-name filter (e.g. "Praveer Nema").
        document_type:         optional "message" or "thread".
        start_timestamp:       optional inclusive lower bound on source timestamps
                               (Slack ts string or unix seconds).
        end_timestamp:         optional inclusive upper bound.
        conversation_history:  optional recent {role, content} turns. Passed
                               to the LLM for reference resolution ("he",
                               "that decision", etc). DOES NOT affect
                               retrieval — only the latest question goes
                               into HydraDB's similarity search.
        metadata_bias:         optional dict like {"channel": "product",
                               "user": "rahul"} — weak inferred filters
                               applied as a ranking bias rather than a
                               hard filter. Strong inference should be
                               passed via `channel`/`user` directly.

    Returns:
        {
            "answer":  str,
            "sources": [...],
            "debug":   { ... }
        }
    """
    if not question or not question.strip():
        return {
            "answer": "Please ask a question.",
            "sources": [],
            "debug": {"reason": "empty question"},
        }

    # IMPORTANT: retrieval uses only the current question. Conversation
    # history is intentionally NOT concatenated into the search query.
    prepared = prepare_recall_context(
        question=question,
        top_k=top_k,
        mode=mode,
        channel=channel,
        user=user,
        document_type=document_type,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        metadata_bias=metadata_bias,
    )

    if not prepared["ready"]:
        return {
            "answer": INSUFFICIENT_CONTEXT_ANSWER,
            "sources": [],
            "debug": prepared["fallback_debug"],
        }

    raw_answer = generate_grounded_answer(
        question=question,
        context=prepared["context_text"],
        mode=mode,
        conversation_history=conversation_history,
    )

    finalized = finalize_answer(
        raw_answer=raw_answer,
        sources=prepared["sources"],
        top_k=top_k,
    )

    history_used = bool(conversation_history)
    return {
        "answer": finalized["answer"],
        "sources": finalized["cleaned_sources"],
        "debug": {
            "chunks_returned":      prepared["chunks_count"],
            "chunks_used":          len(prepared["sources"]),
            "chunks_filtered_out":  prepared["filtered_out"],
            "sources_before_clean": finalized["sources_before"],
            "sources_after_clean":  finalized["sources_after"],
            "mode":                 mode,
            "retrieval_mode":       prepared.get("retrieval_mode", mode),
            "exact_matches_found":  prepared.get("exact_matches", 0),
            "query_terms":          prepared.get("query_terms", []),
            "top_k":                top_k,
            "history_used":         history_used,
            "history_turns":        len(conversation_history) if history_used else 0,
        },
    }