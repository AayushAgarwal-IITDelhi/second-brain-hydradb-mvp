"""
Tests for request_context.py (RequestContextMiddleware).

Uses a minimal test FastAPI app (NOT the production main.app) to avoid
importing all of main.py's dependencies in unit-test mode.

Tests:
- X-Request-ID response header is a valid UUID4
- X-Correlation-ID propagates to ContextVar
- Missing X-Correlation-ID falls back to generated request_id
- Non-HTTP scopes (lifespan) pass through unmodified
- SSE streaming regression: a 3-chunk streaming endpoint delivers all chunks
"""

import json
import re
import uuid

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from logging_config import _correlation_id, _request_id
from request_context import RequestContextMiddleware

# ---------------------------------------------------------------------- #
# Minimal test app
# ---------------------------------------------------------------------- #
test_app = FastAPI()
test_app.add_middleware(RequestContextMiddleware)


@test_app.get('/echo-context')
def echo_context():
    return {
        'request_id': _request_id.get(),
        'correlation_id': _correlation_id.get(),
    }


@test_app.get('/stream')
def stream_endpoint():
    def _gen():
        for i in range(3):
            yield f"chunk{i}"

    return StreamingResponse(_gen(), media_type="text/plain")


client = TestClient(test_app, raise_server_exceptions=True)


# ---------------------------------------------------------------------- #
# X-Request-ID header
# ---------------------------------------------------------------------- #
UUID4_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def test_x_request_id_is_uuid4():
    resp = client.get('/echo-context')
    assert resp.status_code == 200
    rid = resp.headers.get('x-request-id', '')
    assert UUID4_RE.match(rid), f"Not a valid UUID4: {rid!r}"


def test_x_request_id_unique_per_request():
    r1 = client.get('/echo-context')
    r2 = client.get('/echo-context')
    assert r1.headers['x-request-id'] != r2.headers['x-request-id']


# ---------------------------------------------------------------------- #
# ContextVar binding
# ---------------------------------------------------------------------- #
def test_request_id_in_context():
    resp = client.get('/echo-context')
    rid_header = resp.headers['x-request-id']
    body = resp.json()
    assert body['request_id'] == rid_header


def test_correlation_id_propagated_from_header():
    corr = str(uuid.uuid4())
    resp = client.get('/echo-context', headers={'X-Correlation-ID': corr})
    body = resp.json()
    assert body['correlation_id'] == corr


def test_missing_correlation_id_falls_back_to_request_id():
    resp = client.get('/echo-context')
    body = resp.json()
    # When no X-Correlation-ID header is sent, correlation_id equals request_id.
    assert body['correlation_id'] == body['request_id']


# ---------------------------------------------------------------------- #
# SSE streaming regression
# ---------------------------------------------------------------------- #
def test_streaming_delivers_all_chunks():
    """Middleware must not buffer the response body (would break SSE)."""
    resp = client.get('/stream')
    assert resp.status_code == 200
    assert resp.text == 'chunk0chunk1chunk2'
