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
from typing import Any, Dict, List, Optional

from hydradb_client import HydraDBClient
from llm import INSUFFICIENT_CONTEXT_ANSWER, generate_grounded_answer


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
# Public entry point
# ---------------------------------------------------------------------- #
def answer_question(question: str, top_k: int = 5) -> Dict[str, Any]:
    """
    End-to-end recall + grounded answer.

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

    hydra = HydraDBClient()
    raw_response = hydra.full_recall(query=question, top_k=top_k)
    chunks = _extract_chunks(raw_response)

    # ----- Temporary debug printing (handy while we tune extraction) ----- #
    print("[recall] ===== raw HydraDB response (truncated to 8000 chars) =====")
    print(_safe_json(raw_response, limit=8000))
    if chunks:
        print("[recall] ===== first chunk =====")
        print(_safe_json(chunks[0], limit=4000))
    else:
        print("[recall] (no chunks returned)")
    print("[recall] ===========================================================")
    # --------------------------------------------------------------------- #

    # Build numbered context block + parallel source list.
    context_blocks: List[str] = []
    sources: List[Dict[str, Any]] = []
    for i, chunk in enumerate(chunks, start=1):
        text = _chunk_text(chunk).strip()
        if not text:
            continue
        source_id = _chunk_source(chunk, i)
        score = _chunk_score(chunk)
        context_blocks.append(f"[{i}] (source: {source_id})\n{text}")
        sources.append({
            "index": i,
            "source": source_id,
            "score": score,
        })

    if not context_blocks:
        # No usable text in any chunk -> surface enough detail to debug fast.
        first_chunk = chunks[0] if chunks else None
        first_chunk_keys = (
            list(first_chunk.keys()) if isinstance(first_chunk, dict) else None
        )
        return {
            "answer": INSUFFICIENT_CONTEXT_ANSWER,
            "sources": [],
            "debug": {
                "reason": "no usable text found in HydraDB chunks",
                "raw_response_keys": (
                    list(raw_response.keys())
                    if isinstance(raw_response, dict) else None
                ),
                "chunks_returned": len(chunks),
                "first_chunk_keys": first_chunk_keys,
                "first_chunk_preview": (
                    _first_chunk_preview(first_chunk) if first_chunk else None
                ),
                "top_k": top_k,
            },
        }

    context_text = "\n\n".join(context_blocks)
    answer = generate_grounded_answer(question=question, context=context_text)

    return {
        "answer": answer,
        "sources": sources,
        "debug": {
            "chunks_returned": len(chunks),
            "chunks_used": len(context_blocks),
            "top_k": top_k,
        },
    }