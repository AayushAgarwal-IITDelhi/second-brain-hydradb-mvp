"""
FastAPI app for the Second Brain MVP.

Endpoints:
    GET  /            -> service info card             (public)
    GET  /api/health  -> {"status": "ok", ...}         (public)
    POST /api/query   -> {"answer", "sources", ...}    (requires X-API-Key)

CORS: configured for separate frontend dev servers. The browser sends an
OPTIONS preflight before the real request; CORSMiddleware answers it. The
frontend then makes the actual POST. Note for frontend devs: every call
to /api/query must include the header:

    X-API-Key: <APP_API_KEY>

(no cookies / no Authorization header — just this one custom header).

Run with:
    uvicorn main:app --reload --port 8000

Then:
    curl -X POST http://127.0.0.1:8000/api/query \\
        -H "Content-Type: application/json" \\
        -H "X-API-Key: change-me-dev-key" \\
        -d '{"question": "What is the memory layer for the MVP?", "top_k": 5}'
"""

import os
from typing import Any, Dict, List

from dotenv import load_dotenv

# Load .env BEFORE importing modules that read env vars at runtime.
load_dotenv()

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from auth import require_api_key  # noqa: E402
from recall import answer_question  # noqa: E402


# ---------- CORS configuration ---------- #
# Comma-separated list of allowed browser origins. Empty / unset -> sensible
# local dev defaults. We deliberately never default to "*" — that would
# disable browser origin checks entirely for the protected /api/query route.
DEFAULT_CORS_ORIGINS = "http://localhost:3000,http://localhost:5173"


def _parse_cors_origins() -> List[str]:
    raw = os.getenv("CORS_ORIGINS", "").strip() or DEFAULT_CORS_ORIGINS
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


ALLOWED_ORIGINS = _parse_cors_origins()
print(f"[main] CORS allowed origins: {ALLOWED_ORIGINS}")


app = FastAPI(title="Second Brain (Slack MVP)")

# CORSMiddleware sits at the front of the request pipeline. It handles the
# browser's preflight OPTIONS request and tags real responses with the
# right Access-Control-* headers. The frontend must:
#   - send Content-Type: application/json
#   - send X-API-Key: <APP_API_KEY>    (custom header -> triggers preflight)
#   - NOT include credentials (cookies); we set allow_credentials=False.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)


# ---------- Request validation ---------- #
# Validation rules (any violation -> FastAPI's standard 422 response):
#   question: required string, 3-2000 chars AFTER whitespace strip.
#             "  " or "" or "ab" -> 422.
#   top_k:    optional int, 1..10, defaults to 5.
#
# `str_strip_whitespace=True` makes Pydantic strip every string field on
# this model BEFORE its length/regex checks run, so whitespace-only input
# becomes "" and fails min_length=3 with a single, clear error. The
# stripped value is what lands in `req.question`, so downstream code never
# sees padding.
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


# ---------- Public routes ---------- #
@app.get("/")
def root() -> Dict[str, str]:
    """Service info card; useful for quick smoke checks and demos."""
    return {
        "name":   "Second Brain HydraDB MVP",
        "status": "ok",
        "docs":   "/docs",
        "health": "/api/health",
    }


@app.get("/api/health")
def health() -> Dict[str, str]:
    """
    Public health check so external probes (uptime monitors, load
    balancers) don't need the API key. To require auth here too, just
    add `dependencies=[Depends(require_api_key)]` to the decorator above.
    """
    return {"status": "ok", "service": "second-brain-api"}


# ---------- Protected routes ---------- #
# Frontend must send:  X-API-Key: <APP_API_KEY>
# (Defined in auth.py; configured via APP_API_KEY in .env.)
@app.post("/api/query", dependencies=[Depends(require_api_key)])
def query(req: QueryRequest) -> Dict[str, Any]:
    """
    Retrieve Slack context from HydraDB, ask the cloud LLM for a grounded
    answer, and return it along with the source list.

    Requires header:  X-API-Key: <APP_API_KEY>
    """
    return answer_question(question=req.question, top_k=req.top_k)