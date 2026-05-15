"""
FastAPI app for the Second Brain MVP.

Endpoints:
    GET  /api/health -> {"status": "ok"}
    POST /api/query  -> {"answer": ..., "sources": [...], "debug": {...}}

Run with:
    uvicorn main:app --reload --port 8000

Then:
    curl -X POST http://127.0.0.1:8000/api/query \\
        -H "Content-Type: application/json" \\
        -d '{"question": "What is the memory layer for the MVP?", "top_k": 5}'
"""

from typing import Any, Dict

from dotenv import load_dotenv

# Load .env BEFORE importing modules that read env vars at runtime.
load_dotenv()

from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from recall import answer_question  # noqa: E402


app = FastAPI(title="Second Brain (Slack MVP)")


class QueryRequest(BaseModel):
    question: str = Field(..., description="The user's natural-language question.")
    top_k: int = Field(
        5,
        ge=1,
        le=50,
        description="How many context chunks to retrieve from HydraDB.",
    )


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/query")
def query(req: QueryRequest) -> Dict[str, Any]:
    """
    Retrieve Slack context from HydraDB, ask the cloud LLM for a grounded
    answer, and return it along with the source list.
    """
    return answer_question(question=req.question, top_k=req.top_k)