"""
FastAPI app for the Second Brain MVP.

Endpoints:
    GET  /                  -> service info card             (public)
    GET  /api/health        -> {"status": "ok", ...}         (public)
    POST /api/query         -> {"answer", "sources", ...}    (X-API-Key + rate limited)
    POST /api/query/stream  -> Server-Sent Events            (X-API-Key + rate limited)

The frontend MUST send `X-API-Key: <APP_API_KEY>` on every protected call.
"""

import json
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional, Union

from dotenv import load_dotenv

# Load .env BEFORE importing modules that read env vars at runtime.
load_dotenv()

from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from auth import require_api_key  # noqa: E402
from errors import AppError, LLMError, UpstreamTimeoutError, app_error_handler  # noqa: E402
from llm import stream_grounded_answer  # noqa: E402
from prompts import INSUFFICIENT_CONTEXT_ANSWER  # noqa: E402
from query_cache import build_cache_key, get_cached, put as cache_put  # noqa: E402
from rate_limit import rate_limit_dependency  # noqa: E402
from recall import (  # noqa: E402
    answer_question,
    finalize_answer,
    prepare_recall_context,
)
from scheduler import start_scheduler, stop_scheduler  # noqa: E402
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
QueryMode = Literal["default", "summary", "decisions", "action_items", "who_said"]
DocumentType = Literal["message", "thread"]


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
        description="Answer style. 'default' is a concise grounded answer.",
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
    # or a unix-timestamp number. recall.py normalizes both.
    start_timestamp: Optional[Union[str, float]] = Field(
        None,
        description="Inclusive lower bound on source timestamp (Slack ts string or unix seconds).",
    )
    end_timestamp: Optional[Union[str, float]] = Field(
        None,
        description="Inclusive upper bound on source timestamp.",
    )


def _cache_key_from_request(req: QueryRequest) -> str:
    return build_cache_key({
        "question":        req.question,
        "top_k":           req.top_k,
        "mode":            req.mode,
        "channel":         req.channel,
        "user":            req.user,
        "document_type":   req.document_type,
        "start_timestamp": req.start_timestamp,
        "end_timestamp":   req.end_timestamp,
    })


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
    cache_key = _cache_key_from_request(req)
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    result = answer_question(
        question=req.question,
        top_k=req.top_k,
        mode=req.mode,
        channel=req.channel,
        user=req.user,
        document_type=req.document_type,
        start_timestamp=req.start_timestamp,
        end_timestamp=req.end_timestamp,
    )

    # Mark non-cached on the way out so the UI can render a "fresh" badge
    # when it wants to, mirroring the cache-hit case.
    debug = dict(result.get("debug") or {})
    debug["cache_hit"] = False
    result = {**result, "debug": debug}

    # Only cache when we actually produced a useful answer (i.e. context
    # was found). Don't cache the fallback "I do not have enough Slack
    # context" — letting that retry next time is harmless and the answer
    # might change after fresh ingestion.
    if result.get("answer") and result["answer"] != INSUFFICIENT_CONTEXT_ANSWER:
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
    # ----- Cache fast path: same shape as /api/query, just over SSE ---
    cache_key = _cache_key_from_request(req)
    cached = get_cached(cache_key)

    def _generator():
        # If we have a cached result, hand the client the whole answer in
        # one `token` event followed by `done`. The UI renders it the same
        # way as a streamed answer.
        if cached is not None:
            yield _sse_event("token", {"text": cached.get("answer", "")})
            yield _sse_event("done", {
                "answer":  cached.get("answer", ""),
                "sources": cached.get("sources", []),
                "debug":   cached.get("debug", {}),
            })
            return

        # ----- Prepare context first (HydraDB recall + source build) ---
        try:
            prepared = prepare_recall_context(
                question=req.question,
                top_k=req.top_k,
                channel=req.channel,
                user=req.user,
                document_type=req.document_type,
                start_timestamp=req.start_timestamp,
                end_timestamp=req.end_timestamp,
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
            yield _sse_event("done", {
                "answer":  INSUFFICIENT_CONTEXT_ANSWER,
                "sources": [],
                "debug":   {**prepared["fallback_debug"], "cache_hit": False},
            })
            return

        # ----- Stream tokens from the LLM ------------------------------
        accumulated_parts: List[str] = []
        try:
            for piece in stream_grounded_answer(
                question=req.question,
                context=prepared["context_text"],
                mode=req.mode,
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
        full_payload = {
            "answer":  finalized["answer"],
            "sources": finalized["cleaned_sources"],
            "debug": {
                "chunks_returned":      prepared["chunks_count"],
                "chunks_used":          len(prepared["sources"]),
                "chunks_filtered_out":  prepared["filtered_out"],
                "sources_before_clean": finalized["sources_before"],
                "sources_after_clean":  finalized["sources_after"],
                "mode":                 req.mode,
                "top_k":                req.top_k,
                "cache_hit":            False,
            },
        }
        yield _sse_event("done", full_payload)

        # Cache the finalized result for next identical request, same
        # rules as /api/query: don't cache the no-context fallback.
        if full_payload["answer"] and full_payload["answer"] != INSUFFICIENT_CONTEXT_ANSWER:
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