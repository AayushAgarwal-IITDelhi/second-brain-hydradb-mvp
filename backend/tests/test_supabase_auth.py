"""
Tests for auth_supabase.py — Supabase JWT verification.

These tests exercise the REAL require_user dependency (not the
conftest override) by calling it as a plain function. That way we
verify the actual JWT decode logic without needing the FastAPI dep
injection — the override is for the `client` fixture only.
"""

import time
from typing import Any, Dict

import jwt
import pytest
from fastapi import HTTPException

from auth_supabase import (
    SUPABASE_JWT_ALGORITHM,
    SUPABASE_JWT_AUDIENCE,
    SupabaseUser,
    _decode_bearer,
    require_user,
)


JWT_SECRET = "test-jwt-secret"  # must match conftest's env value


def _make_token(
    *,
    sub: str = "user-abc",
    email: str = "alice@example.com",
    exp_delta: int = 3600,
    aud: str = SUPABASE_JWT_AUDIENCE,
    secret: str = JWT_SECRET,
    extra: Dict[str, Any] = None,
) -> str:
    now = int(time.time())
    payload: Dict[str, Any] = {
        "sub": sub,
        "email": email,
        "aud": aud,
        "iat": now,
        "exp": now + exp_delta,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, secret, algorithm=SUPABASE_JWT_ALGORITHM)


class TestRequireUserSuccess:
    def test_valid_token_returns_user(self):
        token = _make_token(sub="user-123", email="bob@example.com")
        user = require_user(authorization=f"Bearer {token}")
        assert isinstance(user, SupabaseUser)
        assert user.id == "user-123"
        assert user.email == "bob@example.com"

    def test_email_can_be_missing(self):
        token = jwt.encode(
            {
                "sub": "user-noemail",
                "aud": SUPABASE_JWT_AUDIENCE,
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
            },
            JWT_SECRET,
            algorithm=SUPABASE_JWT_ALGORITHM,
        )
        user = require_user(authorization=f"Bearer {token}")
        assert user.id == "user-noemail"
        assert user.email is None


class TestRequireUserFailures:
    def test_missing_header_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            require_user(authorization=None)
        assert exc.value.status_code == 401

    def test_empty_header_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            require_user(authorization="")
        assert exc.value.status_code == 401

    def test_non_bearer_scheme_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            require_user(authorization="Basic abc")
        assert exc.value.status_code == 401

    def test_empty_bearer_value_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            require_user(authorization="Bearer ")
        assert exc.value.status_code == 401

    def test_malformed_jwt_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            require_user(authorization="Bearer not.a.jwt")
        assert exc.value.status_code == 401

    def test_expired_token_raises_401(self):
        token = _make_token(exp_delta=-60)
        with pytest.raises(HTTPException) as exc:
            require_user(authorization=f"Bearer {token}")
        assert exc.value.status_code == 401

    def test_wrong_audience_raises_401(self):
        token = _make_token(aud="some-other-audience")
        with pytest.raises(HTTPException) as exc:
            require_user(authorization=f"Bearer {token}")
        assert exc.value.status_code == 401

    def test_wrong_signature_raises_401(self):
        token = _make_token(secret="not-the-real-secret")
        with pytest.raises(HTTPException) as exc:
            require_user(authorization=f"Bearer {token}")
        assert exc.value.status_code == 401

    def test_missing_sub_raises_401(self):
        token = jwt.encode(
            {
                "aud": SUPABASE_JWT_AUDIENCE,
                "iat": int(time.time()),
                "exp": int(time.time()) + 600,
            },
            JWT_SECRET,
            algorithm=SUPABASE_JWT_ALGORITHM,
        )
        with pytest.raises(HTTPException) as exc:
            require_user(authorization=f"Bearer {token}")
        assert exc.value.status_code == 401

    def test_blank_sub_raises_401(self):
        token = _make_token(sub="   ")
        with pytest.raises(HTTPException) as exc:
            require_user(authorization=f"Bearer {token}")
        assert exc.value.status_code == 401


class TestJwtSecretMissing:
    def test_blank_jwt_secret_raises_500(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_JWT_SECRET", "")
        with pytest.raises(HTTPException) as exc:
            require_user(authorization="Bearer anything")
        assert exc.value.status_code == 500


class TestApiQueryAuthIntegration:
    def test_query_without_any_auth_returns_401(self, client):
        r = client.post("/api/query", json={"question": "hello world"})
        assert r.status_code == 401

    def test_query_stream_without_any_auth_returns_401(self, client):
        r = client.post("/api/query/stream", json={"question": "hello world"})
        assert r.status_code == 401

    def test_me_without_auth_returns_401(self, client):
        r = client.get("/api/me")
        assert r.status_code == 401

    def test_me_workspaces_without_auth_returns_401(self, client):
        r = client.get("/api/me/workspaces")
        assert r.status_code == 401