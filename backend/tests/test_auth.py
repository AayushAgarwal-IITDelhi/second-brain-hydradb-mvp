"""
Tests for auth.py — X-API-Key authentication.

In Phase 1 (multi-user via Supabase), `/api/query` and `/api/query/stream`
moved to Supabase JWT auth. The legacy X-API-Key path is kept ONLY for
the internal `/api/admin/status` route. These tests now exercise that
route, preserving end-to-end coverage of the require_api_key dependency.

Tests for the Supabase JWT path live in tests/test_supabase_auth.py.
"""

import os
from unittest.mock import patch

import pytest


class TestRequireApiKey:
    def test_valid_key_passes(self, client, auth_headers):
        resp = client.get("/api/admin/status", headers=auth_headers)
        assert resp.status_code == 200

    def test_missing_key_returns_401(self, client):
        resp = client.get("/api/admin/status")
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, client):
        resp = client.get(
            "/api/admin/status",
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_empty_key_returns_401(self, client):
        resp = client.get(
            "/api/admin/status",
            headers={"X-API-Key": ""},
        )
        assert resp.status_code == 401

    def test_correct_response_shape_on_401(self, client):
        resp = client.get("/api/admin/status")
        assert resp.status_code == 401
        body = resp.json()
        assert "detail" in body

    def test_unconfigured_api_key_fails_closed(self, client):
        """When APP_API_KEY is blank, ALL requests must be rejected."""
        with patch.dict(os.environ, {"APP_API_KEY": ""}):
            resp = client.get(
                "/api/admin/status",
                headers={"X-API-Key": ""},
            )
        assert resp.status_code == 401

    def test_public_endpoints_need_no_key(self, client):
        """/ and /api/health are fully public."""
        assert client.get("/").status_code == 200
        assert client.get("/api/health").status_code == 200


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