"""
FastAPI app for the Second Brain MVP.

Endpoints:
    GET  /                    -> service info card             (public)
    GET  /api/health          -> {"status": "ok", ...}         (public)
    POST /api/query           -> {"answer", "sources", ...}    (X-API-Key + rate limited)
    POST /api/query/stream    -> Server-Sent Events            (X-API-Key + rate limited)
    GET  /api/admin/status    -> ingestion status snapshot     (X-API-Key)
    POST /slack/events        -> Slack Events API webhook      (Slack signature)

The frontend MUST send `X-API-Key: <APP_API_KEY>` on every protected call.
The /slack/events endpoint is public but authenticated via Slack's
HMAC-SHA256 request signature — see slack_signature.py.
"""

import json
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional, Union

from dotenv import load_dotenv

# Load .env BEFORE importing modules that read env vars at runtime.
load_dotenv()

from fastapi import (  # noqa: E402
    BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status,
)
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from auth import require_api_key  # noqa: E402
from date_utils import parse_date_query  # noqa: E402
from errors import AppError, LLMError, UpstreamTimeoutError, app_error_handler  # noqa: E402
from llm import stream_grounded_answer  # noqa: E402
from prompts import INSUFFICIENT_CONTEXT_ANSWER  # noqa: E402
from query_cache import build_cache_key, get_cached, put as cache_put  # noqa: E402
from query_rewriter import rewrite_query  # noqa: E402
from rate_limit import rate_limit_dependency  # noqa: E402
from realtime_ingest import (  # noqa: E402
    admin_status_snapshot,
    _event_already_seen,
    process_slack_event,
)
from recall import (  # noqa: E402
    answer_question,
    finalize_answer,
    prepare_recall_context,
)
from scheduler import auto_ingest_enabled, start_scheduler, stop_scheduler  # noqa: E402
from slack_signature import verify_slack_signature  # noqa: E402
from startup import validate_required_env  # noqa: E402


# ---------- CORS configuration ---------- #
DEFAULT_CORS_ORIGINS = "http://localhost:3000,http://localhost:5173"


def _parse_cors_origins() -> List[str]:
    raw = os.getenv("CORS_ORIGINS", "").strip() or DEFAULT_CORS_ORIGINS
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


ALLOWED_ORIGINS = _parse_cors_origins()
print(f"[main] CORS allowed origins: {ALLOWED_ORIGINS}")


# ---------- Lifespan: startup checks + scheduler ---------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_required_env()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title="Second Brain (Slack MVP)", lifespan=lifespan)
app.add_exception_handler(AppError, app_error_handler)


# ---------- CORS ---------- #
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)


# ---------- Request validation ---------- #
QueryMode = Literal[
    "default", "summary", "decisions", "action_items", "who_said",
    "exact", "hybrid",
]
DocumentType = Literal["message", "thread"]
HistoryRole = Literal["user", "assistant"]


# Cap on conversation_history entries from the request. Frontend keeps the
# last 6; we enforce the same on the server as defense in depth — a buggy
# or malicious client can't blow up the prompt with thousands of turns.
MAX_CONVERSATION_HISTORY = 6


class ConversationMessage(BaseModel):
    """One turn of recent chat history sent by the frontend."""
    model_config = ConfigDict(str_strip_whitespace=True)

    role: HistoryRole = Field(
        ...,
        description="Speaker of this turn: 'user' or 'assistant'.",
    )
    content: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="The turn's text. Long assistant answers are truncated "
                    "downstream when formatted into the prompt.",
    )


class QueryRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="The user's natural-language question (3–2000 chars, "
                    "leading/trailing whitespace is stripped).",
    )
    top_k: int = Field(
        5,
        ge=1,
        le=10,
        description="How many context chunks to retrieve from HydraDB (1–10).",
    )
    mode: QueryMode = Field(
        "default",
        description="Answer style + retrieval strategy. 'default' is a "
                    "concise grounded answer; 'exact' prefers literal "
                    "keyword matches; 'hybrid' combines semantic + keyword.",
    )
    channel: Optional[str] = Field(
        None, max_length=200,
        description="Optional channel-name filter (e.g. 'all-second-brain').",
    )
    user: Optional[str] = Field(
        None, max_length=200,
        description="Optional user-name filter.",
    )
    document_type: Optional[DocumentType] = Field(
        None,
        description="Optional filter: 'message' or 'thread'.",
    )
    # Date range — accept either a Slack ts string ("1778775842.876209")
    # or a unix-timestamp number. recall.py normalizes both. Explicit
    # values override anything derived from date_query.
    start_timestamp: Optional[Union[str, float]] = Field(
        None,
        description="Inclusive lower bound on source timestamp (Slack ts string or unix seconds).",
    )
    end_timestamp: Optional[Union[str, float]] = Field(
        None,
        description="Inclusive upper bound on source timestamp.",
    )
    # Natural-language date phrase (e.g. "last week", "yesterday",
    # "after May 10"). Parsed server-side; explicit start/end_timestamp
    # win where they overlap.
    date_query: Optional[str] = Field(
        None, max_length=200,
        description="Natural-language date phrase. Examples: 'today', "
                    "'last week', 'last 7 days', 'after May 10', "
                    "'from May 1 to May 7'.",
    )
    # Recent chat turns (oldest first). Used by the LLM to resolve
    # references like 'he' / 'that' / 'the earlier discussion'. Does NOT
    # affect HydraDB retrieval — only the latest `question` does.
    # Server caps at MAX_CONVERSATION_HISTORY turns regardless of what
    # the client sends.
    conversation_history: Optional[List[ConversationMessage]] = Field(
        None,
        description="Recent chat turns (oldest first), up to "
                    f"{MAX_CONVERSATION_HISTORY}. Anything beyond that is "
                    "truncated server-side to the most recent turns.",
    )


def _resolve_date_filters(req: QueryRequest) -> Dict[str, Any]:
    """
    Resolve the request's date inputs into a single effective range
    plus a debug record. Explicit start/end_timestamp always win.

    Returns:
        {
            "start_timestamp": float | str | None,    # effective start
            "end_timestamp":   float | str | None,    # effective end
            "date_query_debug": {                     # surfaced in /api debug
                "phrase":   str,
                "matched":  bool,
                "note":     str,
                "applied_start": float | None,
                "applied_end":   float | None,
            } | None,
        }
    """
    explicit_start = req.start_timestamp
    explicit_end = req.end_timestamp

    parsed = parse_date_query(req.date_query)
    if not parsed["phrase"]:
        # User didn't send date_query at all. No debug info needed.
        return {
            "start_timestamp": explicit_start,
            "end_timestamp":   explicit_end,
            "date_query_debug": None,
        }

    # Explicit values win. We only apply parsed bounds where the explicit
    # value is None, so a request that sets both keeps full control.
    effective_start = explicit_start if explicit_start is not None else parsed["start_timestamp"]
    effective_end = explicit_end if explicit_end is not None else parsed["end_timestamp"]

    return {
        "start_timestamp": effective_start,
        "end_timestamp":   effective_end,
        "date_query_debug": {
            "phrase":         parsed["phrase"],
            "matched":        parsed["matched"],
            "note":           parsed["note"],
            "applied_start":  parsed["start_timestamp"] if explicit_start is None else None,
            "applied_end":    parsed["end_timestamp"]   if explicit_end is None   else None,
        },
    }


def _cache_key_from_request(
    req: QueryRequest,
    resolved: Dict[str, Any],
    rewrite: Dict[str, Any],
) -> str:
    """
    Build a cache key from the request + resolved date range + resolved
    query-rewrite filters.

    Including the resolved date range (not the raw date_query phrase) means
    two requests with different phrasings of the same window — "last week"
    vs "from <last Monday> to <last Sunday>" — both share a cache slot.

    Including the effective_channel/effective_user/metadata_bias from the
    query rewriter means two queries that look textually similar but
    resolve to different inferred filters get different cache slots. The
    `metadata_bias` is also keyed because weak inference still changes
    the ranking order.
    """
    return build_cache_key({
        "question":        req.question,
        "top_k":           req.top_k,
        "mode":            req.mode,
        # Use the EFFECTIVE filters (explicit OR strong-inferred) so an
        # inferred filter cache-isolates from an unfiltered query.
        "channel":         rewrite["effective_channel"],
        "user":            rewrite["effective_user"],
        "document_type":   req.document_type,
        "start_timestamp": resolved["start_timestamp"],
        "end_timestamp":   resolved["end_timestamp"],
        # Weak inference: metadata_bias is a dict or None.
        "metadata_bias":   rewrite["metadata_bias"],
    })


def _normalize_history(req: QueryRequest) -> List[Dict[str, str]]:
    """
    Convert the request's conversation_history (Pydantic models) into a
    plain list-of-dicts capped at MAX_CONVERSATION_HISTORY, oldest-first.

    Defensive on the server side:
      - If the client sends more than the cap, we keep only the most
        recent turns.
      - Empty/whitespace content is dropped (the Pydantic min_length=1
        catches truly empty strings, but a whitespace-only string would
        slip through and we'd rather skip it than show "User: " to the LLM).

    Returns an empty list when no usable history was provided, which the
    downstream "history_used" flag treats as "no history" — same as if
    the client had omitted the field entirely.
    """
    raw = req.conversation_history or []
    cleaned: List[Dict[str, str]] = []
    for msg in raw:
        content = (msg.content or "").strip()
        if not content:
            continue
        cleaned.append({"role": msg.role, "content": content})
    # Keep the most recent N if the client over-sent.
    if len(cleaned) > MAX_CONVERSATION_HISTORY:
        cleaned = cleaned[-MAX_CONVERSATION_HISTORY:]
    return cleaned


def _resolve_query_rewrite(req: QueryRequest) -> Dict[str, Any]:
    """
    Run person/channel inference on the question and split the results
    into "effective filters" and "metadata bias":

      strong inference -> becomes the effective filter (hard filter)
      weak inference   -> becomes the metadata bias (ranking-only)

    Explicit request filters always win — if the user sent `channel: ...`,
    inference for channel is suppressed entirely. Same for `user`.

    Returns:
        {
            "effective_channel": str | None,   # what prepare_recall sees
            "effective_user":    str | None,
            "metadata_bias":     {channel?, user?} | None,
            "rewrite_debug":     {                  # surfaced to UI
                "inferred_person":   str | None,
                "inferred_channel":  str | None,
                "person_confidence": "strong" | "weak" | None,
                "channel_confidence":"strong" | "weak" | None,
                "retrieval_biases_applied": list[str],
            } | None,
        }
    """
    rewrite = rewrite_query(
        req.question,
        explicit_channel=req.channel,
        explicit_user=req.user,
    )

    effective_channel = req.channel
    effective_user = req.user
    bias: Dict[str, str] = {}

    # Strong inference → hard filter (only when caller didn't set one).
    if (rewrite["inferred_channel"]
            and rewrite["channel_confidence"] == "strong"
            and not (req.channel and req.channel.strip())):
        effective_channel = rewrite["inferred_channel"]
    elif (rewrite["inferred_channel"]
            and rewrite["channel_confidence"] == "weak"
            and not (req.channel and req.channel.strip())):
        bias["channel"] = rewrite["inferred_channel"]

    if (rewrite["inferred_person"]
            and rewrite["person_confidence"] == "strong"
            and not (req.user and req.user.strip())):
        effective_user = rewrite["inferred_person"]
    elif (rewrite["inferred_person"]
            and rewrite["person_confidence"] == "weak"
            and not (req.user and req.user.strip())):
        bias["user"] = rewrite["inferred_person"]

    rewrite_debug = None
    if rewrite["inferred_person"] or rewrite["inferred_channel"]:
        # Only attach the debug record when we actually inferred something —
        # otherwise the UI would have to special-case empty rewrite blobs.
        rewrite_debug = {
            "inferred_person":          rewrite["inferred_person"],
            "inferred_channel":         rewrite["inferred_channel"],
            "person_confidence":        rewrite["person_confidence"],
            "channel_confidence":       rewrite["channel_confidence"],
            "retrieval_biases_applied": rewrite["retrieval_biases_applied"],
        }

    return {
        "effective_channel": effective_channel,
        "effective_user":    effective_user,
        "metadata_bias":     bias or None,
        "rewrite_debug":     rewrite_debug,
    }


# ---------- Public routes ---------- #
@app.get("/")
def root() -> Dict[str, str]:
    return {
        "name":   "Second Brain HydraDB MVP",
        "status": "ok",
        "docs":   "/docs",
        "health": "/api/health",
    }


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "service": "second-brain-api"}


# ---------- Protected routes ---------- #
@app.post(
    "/api/query",
    dependencies=[Depends(require_api_key), Depends(rate_limit_dependency)],
)
def query(req: QueryRequest) -> Dict[str, Any]:
    """
    Retrieve Slack context from HydraDB, ask the cloud LLM for a grounded
    answer, and return it along with the source list.

    Cached responses are flagged via debug.cache_hit=true.

    Requires header:  X-API-Key: <APP_API_KEY>
    """
    resolved = _resolve_date_filters(req)
    history = _normalize_history(req)
    rewrite = _resolve_query_rewrite(req)

    # Cache behavior when conversation_history is present:
    #   We BYPASS the cache entirely. Reasons:
    #     1. Two follow-ups in different conversations may share the same
    #        question text ("what did he say?") but mean different things.
    #     2. A long history makes for a wide cache key with near-zero hit
    #        rate.
    #     3. Stateless queries (no history) keep working through the cache
    #        exactly as before — no regression.
    use_cache = not history
    cache_key = (
        _cache_key_from_request(req, resolved, rewrite) if use_cache else None
    )

    if use_cache:
        cached = get_cached(cache_key)
        if cached is not None:
            return cached

    result = answer_question(
        question=req.question,
        top_k=req.top_k,
        mode=req.mode,
        # Effective filters: explicit > strong-inferred > None.
        channel=rewrite["effective_channel"],
        user=rewrite["effective_user"],
        document_type=req.document_type,
        start_timestamp=resolved["start_timestamp"],
        end_timestamp=resolved["end_timestamp"],
        conversation_history=history,
        # Weak inference becomes a ranking bias (matching chunks float up
        # but non-matching chunks aren't dropped).
        metadata_bias=rewrite["metadata_bias"],
    )

    # Mark non-cached on the way out so the UI can render a "fresh" badge
    # when it wants to, mirroring the cache-hit case. Attach the
    # date_query parsing record too so the UI can show "matched 'last week'".
    debug = dict(result.get("debug") or {})
    debug["cache_hit"] = False
    if resolved["date_query_debug"] is not None:
        debug["date_query"] = resolved["date_query_debug"]
    if rewrite["rewrite_debug"] is not None:
        debug["query_rewrite"] = rewrite["rewrite_debug"]
    # Surface cache bypass reason so the UI can show why a follow-up
    # didn't hit the cache.
    if history:
        debug["cache_bypassed"] = "conversation_history present"
    result = {**result, "debug": debug}

    # Only cache when we actually produced a useful answer (i.e. context
    # was found) AND there's no conversation history. Don't cache the
    # fallback string either — letting it retry next time is harmless
    # and the answer might change after fresh ingestion.
    if use_cache and result.get("answer") \
            and result["answer"] != INSUFFICIENT_CONTEXT_ANSWER:
        cache_put(cache_key, result)
    return result


# ---------- Streaming variant ---------- #
def _sse_event(event: str, data: Any) -> bytes:
    """
    Format one SSE event. Both `event:` and `data:` lines are required for
    named events. The blank line ends the message. We always JSON-encode
    `data` so the frontend has a uniform parse path.
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


@app.post(
    "/api/query/stream",
    dependencies=[Depends(require_api_key), Depends(rate_limit_dependency)],
)
def query_stream(req: QueryRequest, request: Request) -> StreamingResponse:
    """
    Same request schema as POST /api/query, but the answer is streamed
    via Server-Sent Events. Event types:

      event: token   data: { "text": "<delta>" }
      event: done    data: { "answer": str, "sources": [...], "debug": {...} }
      event: error   data: { "detail": "...", "error_type": "..." }

    The frontend assembles tokens into the running answer and uses the
    final `done` event for source metadata. Cached responses are served
    by emitting a single `done` event immediately.
    """
    # ----- Resolve date inputs, history, query rewrite, cache key ----
    resolved = _resolve_date_filters(req)
    history = _normalize_history(req)
    rewrite = _resolve_query_rewrite(req)

    # Same bypass rule as /api/query: when conversation_history is set,
    # don't use or write the cache. Stateless streams still cache.
    use_cache = not history
    cache_key = (
        _cache_key_from_request(req, resolved, rewrite) if use_cache else None
    )
    cached = get_cached(cache_key) if use_cache else None

    # Snapshot the date_query + rewrite debug records once; both the
    # cached and the fresh paths attach them to their `done` event.
    date_query_debug = resolved["date_query_debug"]
    rewrite_debug = rewrite["rewrite_debug"]

    def _generator():
        # If we have a cached result, hand the client the whole answer in
        # one `token` event followed by `done`. The UI renders it the same
        # way as a streamed answer.
        if cached is not None:
            yield _sse_event("token", {"text": cached.get("answer", "")})
            cached_debug = dict(cached.get("debug", {}) or {})
            if date_query_debug is not None:
                cached_debug["date_query"] = date_query_debug
            if rewrite_debug is not None:
                cached_debug["query_rewrite"] = rewrite_debug
            yield _sse_event("done", {
                "answer":  cached.get("answer", ""),
                "sources": cached.get("sources", []),
                "debug":   cached_debug,
            })
            return

        # ----- Prepare context first (HydraDB recall + source build) ---
        # NOTE: retrieval uses only the current question, never the
        # history — same invariant as /api/query.
        try:
            prepared = prepare_recall_context(
                question=req.question,
                top_k=req.top_k,
                mode=req.mode,
                channel=rewrite["effective_channel"],
                user=rewrite["effective_user"],
                document_type=req.document_type,
                start_timestamp=resolved["start_timestamp"],
                end_timestamp=resolved["end_timestamp"],
                metadata_bias=rewrite["metadata_bias"],
            )
        except AppError as e:
            yield _sse_event("error", {
                "detail": e.detail, "error_type": e.error_type,
            })
            return
        except Exception as e:  # noqa: BLE001
            yield _sse_event("error", {
                "detail": "Unexpected server error.",
                "error_type": "internal_error",
            })
            print(f"[query_stream] unexpected: {type(e).__name__}: {e}")
            return

        if not prepared["ready"]:
            # No context found — emit the canonical fallback as one token,
            # then done. This keeps the frontend's state machine simple.
            yield _sse_event("token", {"text": INSUFFICIENT_CONTEXT_ANSWER})
            fallback_debug = {**prepared["fallback_debug"], "cache_hit": False}
            if date_query_debug is not None:
                fallback_debug["date_query"] = date_query_debug
            if rewrite_debug is not None:
                fallback_debug["query_rewrite"] = rewrite_debug
            if history:
                fallback_debug["cache_bypassed"] = "conversation_history present"
            yield _sse_event("done", {
                "answer":  INSUFFICIENT_CONTEXT_ANSWER,
                "sources": [],
                "debug":   fallback_debug,
            })
            return

        # ----- Stream tokens from the LLM ------------------------------
        # `conversation_history` is passed to the LLM so it can resolve
        # references in the latest question. Retrieval already finished
        # using only the question.
        accumulated_parts: List[str] = []
        try:
            for piece in stream_grounded_answer(
                question=req.question,
                context=prepared["context_text"],
                mode=req.mode,
                conversation_history=history,
            ):
                accumulated_parts.append(piece)
                yield _sse_event("token", {"text": piece})
        except AppError as e:
            yield _sse_event("error", {
                "detail": e.detail, "error_type": e.error_type,
            })
            return
        except Exception as e:  # noqa: BLE001
            yield _sse_event("error", {
                "detail": "Unexpected LLM error.",
                "error_type": "internal_error",
            })
            print(f"[query_stream] llm unexpected: {type(e).__name__}: {e}")
            return

        # ----- Finalize and emit done ---------------------------------
        raw_answer = "".join(accumulated_parts).strip()
        finalized = finalize_answer(
            raw_answer=raw_answer,
            sources=prepared["sources"],
            top_k=req.top_k,
        )
        debug = {
            "chunks_returned":      prepared["chunks_count"],
            "chunks_used":          len(prepared["sources"]),
            "chunks_filtered_out":  prepared["filtered_out"],
            "sources_before_clean": finalized["sources_before"],
            "sources_after_clean":  finalized["sources_after"],
            "mode":                 req.mode,
            "retrieval_mode":       prepared.get("retrieval_mode", req.mode),
            "exact_matches_found":  prepared.get("exact_matches", 0),
            "query_terms":          prepared.get("query_terms", []),
            "top_k":                req.top_k,
            "cache_hit":            False,
            "history_used":         bool(history),
            "history_turns":        len(history),
        }
        if date_query_debug is not None:
            debug["date_query"] = date_query_debug
        if rewrite_debug is not None:
            debug["query_rewrite"] = rewrite_debug
        if history:
            debug["cache_bypassed"] = "conversation_history present"
        full_payload = {
            "answer":  finalized["answer"],
            "sources": finalized["cleaned_sources"],
            "debug":   debug,
        }
        yield _sse_event("done", full_payload)

        # Cache the finalized result for next identical request, same
        # rules as /api/query: only when stateless AND not the no-context
        # fallback.
        if use_cache and full_payload["answer"] \
                and full_payload["answer"] != INSUFFICIENT_CONTEXT_ANSWER:
            cache_put(cache_key, full_payload)

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={
            # Disable buffering on proxies/nginx so events arrive immediately.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------- Slack Events API webhook ---------- #
# Public route — authenticated by Slack's HMAC signature, not X-API-Key.
# Slack expects us to ACK within 3 seconds, so we verify, queue the
# ingestion as a BackgroundTask, and return 200 immediately.
@app.post("/slack/events")
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_signature: Optional[str] = Header(default=None, alias="X-Slack-Signature"),
    x_slack_request_timestamp: Optional[str] = Header(
        default=None, alias="X-Slack-Request-Timestamp"
    ),
):
    # Read body BEFORE parsing so we can verify the signature over the
    # exact bytes Slack sent. Reparsing the JSON to bytes would change
    # whitespace and break the HMAC.
    body = await request.body()

    if not verify_slack_signature(body, x_slack_request_timestamp, x_slack_signature):
        # Slack will keep retrying briefly. The 401 makes the failure
        # visible in Slack's event dashboard so misconfigurations show up
        # there instead of disappearing into the logs.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Slack signature.",
        )

    # Parse the body now that we trust it. If the body isn't valid JSON
    # we return 400 — Slack should never send anything else, but it's the
    # right response.
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body is not valid JSON.",
        )

    # ----- URL verification handshake -----
    # When you add or edit the Events Subscription URL in Slack, Slack
    # POSTs {"type": "url_verification", "challenge": "..."} and expects
    # us to echo the challenge back as plain text (or as {"challenge": "..."}).
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge", "")
        return PlainTextResponse(content=challenge, status_code=200)

    # ----- Event callback -----
    if payload.get("type") == "event_callback":
        # Idempotency: Slack retries failed deliveries up to 3 times within
        # an hour, so we dedupe by event_id. The webhook still ACKs 200 on
        # duplicates so Slack stops retrying.
        event_id = payload.get("event_id") or ""
        if _event_already_seen(event_id):
            print(f"[slack/events] duplicate event_id={event_id}; ack'd")
            return JSONResponse(content={"ok": True})

        event = payload.get("event") or {}
        # Dispatch ingestion in the background so Slack gets its 200 ack
        # within 3 seconds even if HydraDB / Slack API calls are slow.
        background_tasks.add_task(process_slack_event, event)
        return JSONResponse(content={"ok": True})

    # Unknown top-level type. Ack 200 so Slack stops retrying.
    print(f"[slack/events] ignoring payload type={payload.get('type')!r}")
    return JSONResponse(content={"ok": True})


# ---------- Admin status (light, read-only) ---------- #
@app.get(
    "/api/admin/status",
    dependencies=[Depends(require_api_key)],
)
def admin_status() -> Dict[str, Any]:
    """
    Lightweight ingestion-status snapshot for the frontend admin card.
    Does NOT expose any per-document content — only counters and flags.
    """
    return admin_status_snapshot(scheduler_enabled=auto_ingest_enabled())