"""
Shared pytest fixtures for the Second Brain test suite.

Sets up required env vars BEFORE any app imports so modules that read env
at import time (e.g. ingest_slack.py constants) see the test values.
All external systems (HydraDB, OpenAI, Slack) are mocked — no real network
calls are made and no real credentials are needed.
"""

import os
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

# ── Bootstrap sys.path so `import main` works from the tests/ directory ──
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ── Set required env vars BEFORE any app module is imported ──────────────
# These must be set early because some modules read env vars at module-load
# time (e.g. ingest_slack.py's MESSAGES_PER_CHANNEL / UPLOAD_BATCH_SIZE).
os.environ.setdefault("APP_API_KEY", "test-secret-key")
os.environ.setdefault("HYDRADB_API_KEY", "test-hydradb-key")
os.environ.setdefault("HYDRADB_TENANT_ID", "test-tenant-id")
os.environ.setdefault("HYDRADB_SUB_TENANT_ID", "test-sub-tenant")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-slack-signing-secret")
os.environ.setdefault("SLACK_CHANNEL_IDS", "C123456,C789012")
# Disable the query cache in tests so each test sees live responses.
os.environ["QUERY_CACHE_ENABLED"] = "false"
# Keep the scheduler off so tests don't spin up APScheduler threads.
os.environ["AUTO_INGEST"] = "false"
# Raise the rate limit so ordinary tests don't trip it.
os.environ["RATE_LIMIT_PER_5_MIN"] = "10000"


# ── Lazy app import (after env is set) ───────────────────────────────────
# We patch the lifespan-called functions so TestClient startup doesn't try
# to talk to real services.
@pytest.fixture(scope="session")
def _patched_app():
    """Import the FastAPI app with lifespan hooks patched out."""
    with (
        patch("startup.validate_required_env"),
        patch("scheduler.start_scheduler"),
        patch("scheduler.stop_scheduler"),
    ):
        import main as _main  # noqa: PLC0415
        return _main.app


@pytest.fixture()
def client(_patched_app):
    """Per-test TestClient that triggers the (mocked) lifespan."""
    from fastapi.testclient import TestClient

    with (
        patch("main.validate_required_env"),
        patch("main.start_scheduler"),
        patch("main.stop_scheduler"),
    ):
        with TestClient(_patched_app, raise_server_exceptions=True) as c:
            yield c


# ── Common header helpers ─────────────────────────────────────────────────
TEST_API_KEY = "test-secret-key"
AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


@pytest.fixture()
def auth_headers():
    return dict(AUTH_HEADERS)


# ── HydraDB mock ──────────────────────────────────────────────────────────
def _make_hydra_chunk(
    text: str = "This is a test chunk from Slack.",
    source_id: str = "doc-abc",
    filename: str = "slack_general_1000000000.md",
    score: float = 0.95,
    channel: str = "general",
    stable_key: str = "slack:C123:1000000000.000000",
) -> dict:
    return {
        "text": text,
        "score": score,
        "source_id": source_id,
        "filename": filename,
        "metadata": {
            "channel": channel,
            "stable_key": stable_key,
        },
    }


@pytest.fixture()
def mock_hydra_response():
    """A minimal but valid HydraDB recall response."""
    return {
        "chunks": [
            _make_hydra_chunk(
                text="Alice said the sprint deadline is Friday.",
                source_id="doc-001",
                stable_key="slack:C123:1000000001.000000",
            ),
            _make_hydra_chunk(
                text="Bob confirmed the API design in the meeting.",
                source_id="doc-002",
                stable_key="slack:C123:1000000002.000000",
            ),
        ]
    }


@pytest.fixture()
def mock_hydra_empty():
    """HydraDB response with no usable chunks."""
    return {"chunks": []}


# ── LLM mock ─────────────────────────────────────────────────────────────
@pytest.fixture()
def mock_llm_answer():
    return "Alice mentioned the sprint deadline is Friday [1]."


# ── Ingestion state helpers ───────────────────────────────────────────────
@pytest.fixture()
def tmp_state_path(tmp_path):
    """A temporary ingestion_state.json path (file does NOT exist yet)."""
    return tmp_path / "ingestion_state.json"


@pytest.fixture()
def populated_state_path(tmp_path):
    """A pre-populated ingestion state file for lookup tests."""
    import json

    state = {
        "version": 2,
        "entries": {
            "slack:C123:1000000001.000000": {
                "stable_key": "slack:C123:1000000001.000000",
                "filename": "slack_general_1000000001.md",
                "source_id": "doc-001",
                "channel_id": "C123",
                "channel_name": "general",
                "ts": "1000000001.000000",
                "thread_ts": None,
                "uploaded_at": "2026-01-01T00:00:00+00:00",
                "user_name": "Alice",
                "timestamp": "1000000001.000000",
                "snippet": "Sprint deadline is Friday",
                "permalink": "https://slack.com/archives/C123/p1000000001000000",
                "document_type": "message",
            },
            "slack_thread:C123:2000000001.000000": {
                "stable_key": "slack_thread:C123:2000000001.000000",
                "filename": "slack_general_2000000001.md",
                "source_id": "doc-002",
                "channel_id": "C123",
                "channel_name": "general",
                "ts": None,
                "thread_ts": "2000000001.000000",
                "uploaded_at": "2026-01-02T00:00:00+00:00",
                "user_name": "Bob",
                "timestamp": "2000000001.000000",
                "snippet": "API design finalized",
                "permalink": "https://slack.com/archives/C123/p2000000001000000",
                "document_type": "thread",
            },
        },
        "channels": {
            "C123": {"last_synced_ts": "2000000001.000000"},
            "_meta": {"last_ingested_at": "2026-01-02T00:00:00+00:00"},
        },
    }
    p = tmp_path / "ingestion_state.json"
    p.write_text(json.dumps(state))
    return p
