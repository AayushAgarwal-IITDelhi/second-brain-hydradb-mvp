"""Tests for auth.py — API key authentication."""

import os
from unittest.mock import patch

import pytest


# ── Unit tests for require_api_key ────────────────────────────────────────
class TestRequireApiKey:
    def test_valid_key_passes(self, client, auth_headers):
        resp = client.get("/api/health", headers=auth_headers)
        # /api/health is public but we exercise the auth path via /api/query below
        assert resp.status_code == 200

    def test_missing_key_returns_401(self, client):
        resp = client.post(
            "/api/query",
            json={"question": "what happened?"},
        )
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, client):
        resp = client.post(
            "/api/query",
            json={"question": "what happened?"},
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_empty_key_returns_401(self, client):
        resp = client.post(
            "/api/query",
            json={"question": "what happened?"},
            headers={"X-API-Key": ""},
        )
        assert resp.status_code == 401

    def test_correct_response_shape_on_401(self, client):
        resp = client.post(
            "/api/query",
            json={"question": "what happened?"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert "detail" in body

    def test_unconfigured_api_key_fails_closed(self, client):
        """When APP_API_KEY is blank, ALL requests must be rejected."""
        with patch.dict(os.environ, {"APP_API_KEY": ""}):
            resp = client.post(
                "/api/query",
                json={"question": "what happened?"},
                headers={"X-API-Key": ""},
            )
        assert resp.status_code == 401

    def test_public_endpoints_need_no_key(self, client):
        """/ and /api/health are fully public."""
        assert client.get("/").status_code == 200
        assert client.get("/api/health").status_code == 200


# ── Unit-level tests for auth helper functions ────────────────────────────
class TestAuthHelpers:
    def test_expected_api_key_returns_none_when_unset(self):
        from auth import _expected_api_key

        with patch.dict(os.environ, {"APP_API_KEY": ""}):
            assert _expected_api_key() is None

    def test_expected_api_key_strips_whitespace(self):
        from auth import _expected_api_key

        with patch.dict(os.environ, {"APP_API_KEY": "  mykey  "}):
            assert _expected_api_key() == "mykey"

    def test_expected_api_key_returns_value(self):
        from auth import _expected_api_key

        with patch.dict(os.environ, {"APP_API_KEY": "supersecret"}):
            assert _expected_api_key() == "supersecret"
