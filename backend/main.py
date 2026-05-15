"""
FastAPI app for the Second Brain MVP.

Endpoints:
    GET  /            -> service info card             (public)
    GET  /api/health  -> {"status": "ok", ...}         (public)
    POST /api/query   -> {"answer", "sources", ...}    (X-API-Key + rate limited)

The frontend MUST send `X-API-Key: <APP_API_KEY>` on every protected call.

Run with:
    uvicorn main:app --reload --port 8000
"""

import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv

# Load .env BEFORE importing modules that read env vars at runtime.
load_dotenv()

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from auth import require_api_key  # noqa: E402
from errors import AppError, app_error_handler  # noqa: E402
from rate_limit import rate_limit_dependency  # noqa: E402
from recall import answer_question  # noqa: E402
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
    """
    Validate required env vars and start the optional ingestion scheduler.
    If config is missing, validate_required_env raises and uvicorn aborts
    with a clear, multi-line message.
    """
    validate_required_env()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title="Second Brain (Slack MVP)", lifespan=lifespan)


# Translate every typed AppError into a normalized JSON 4xx/5xx response.
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
# Modes supported by the LLM prompt layer; Pydantic Literal enforces the set
# so anything else -> 422 automatically.
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
        None,
        max_length=200,
        description="Optional channel-name filter (e.g. 'all-second-brain').",
    )
    user: Optional[str] = Field(
        None,
        max_length=200,
        description="Optional user-name filter (e.g. 'Praveer Nema').",
    )
    document_type: Optional[DocumentType] = Field(
        None,
        description="Optional filter: 'message' or 'thread'.",
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
    """Public health check so external probes don't need the API key."""
    return {"status": "ok", "service": "second-brain-api"}


# ---------- Protected routes ---------- #
# Frontend must send:  X-API-Key: <APP_API_KEY>
# Rate limited per X-API-Key (or per IP if header absent).
@app.post(
    "/api/query",
    dependencies=[Depends(require_api_key), Depends(rate_limit_dependency)],
)
def query(req: QueryRequest) -> Dict[str, Any]:
    """
    Retrieve Slack context from HydraDB, ask the cloud LLM for a grounded
    answer, and return it along with the source list.

    Requires header:  X-API-Key: <APP_API_KEY>
    """
    return answer_question(
        question=req.question,
        top_k=req.top_k,
        mode=req.mode,
        channel=req.channel,
        user=req.user,
        document_type=req.document_type,
    )