"""
Tests for the workspace dependency and /api/me/workspaces.

Like test_supabase_auth.py, these call require_workspace directly so we
exercise the real membership-lookup path (with the Supabase client
patched). The /api endpoint tests use the conftest dependency override
plus a patch on list_user_workspaces.
"""

import time
from unittest.mock import patch

import jwt
import pytest
from fastapi import HTTPException

from auth_supabase import (
    SUPABASE_JWT_ALGORITHM,
    SUPABASE_JWT_AUDIENCE,
    WorkspaceContext,
    require_workspace,
)

JWT_SECRET = "test-jwt-secret"
WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"


def _bearer(sub: str = "user-abc") -> str:
    payload = {
        "sub": sub,
        "aud": SUPABASE_JWT_AUDIENCE,
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    return "Bearer " + jwt.encode(
        payload,
        JWT_SECRET,
        algorithm=SUPABASE_JWT_ALGORITHM,
    )


class TestRequireWorkspaceSuccess:
    def test_member_returns_workspace_context(self):
        with patch("auth_supabase.get_workspace_membership", return_value="member"):
            ctx = require_workspace(
                authorization=_bearer(),
                x_workspace_id=WORKSPACE_ID,
            )
        assert isinstance(ctx, WorkspaceContext)
        assert ctx.workspace_id == WORKSPACE_ID
        assert ctx.role == "member"
        assert ctx.user.id == "user-abc"

    def test_owner_role_propagates(self):
        with patch("auth_supabase.get_workspace_membership", return_value="owner"):
            ctx = require_workspace(
                authorization=_bearer(),
                x_workspace_id=WORKSPACE_ID,
            )
        assert ctx.role == "owner"


class TestRequireWorkspaceFailures:
    def test_missing_workspace_header_returns_400(self):
        with pytest.raises(HTTPException) as exc:
            require_workspace(authorization=_bearer(), x_workspace_id=None)
        assert exc.value.status_code == 400

    def test_blank_workspace_header_returns_400(self):
        with pytest.raises(HTTPException) as exc:
            require_workspace(authorization=_bearer(), x_workspace_id="   ")
        assert exc.value.status_code == 400

    def test_non_member_returns_403(self):
        with patch("auth_supabase.get_workspace_membership", return_value=None):
            with pytest.raises(HTTPException) as exc:
                require_workspace(
                    authorization=_bearer(),
                    x_workspace_id=WORKSPACE_ID,
                )
        assert exc.value.status_code == 403

    def test_invalid_token_returns_401_before_workspace_check(self):
        with pytest.raises(HTTPException) as exc:
            require_workspace(
                authorization="Bearer not.a.jwt",
                x_workspace_id=WORKSPACE_ID,
            )
        assert exc.value.status_code == 401


class TestMyWorkspacesEndpoint:
    def test_returns_workspaces_list(self, client, jwt_auth_headers):
        fake_rows = [
            {"id": "ws-001", "name": "Alice's workspace", "slug": "alice", "role": "owner"},
            {"id": "ws-002", "name": "Shared project", "slug": "shared", "role": "member"},
        ]
        with patch("main.list_user_workspaces", return_value=fake_rows):
            r = client.get("/api/me/workspaces", headers=jwt_auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert len(body) == 2
        assert body[0]["slug"] == "alice"
        assert body[1]["role"] == "member"

    def test_empty_when_no_memberships(self, client, jwt_auth_headers):
        with patch("main.list_user_workspaces", return_value=[]):
            r = client.get("/api/me/workspaces", headers=jwt_auth_headers)
        assert r.status_code == 200
        assert r.json() == []


class TestMeEndpoint:
    def test_returns_id_and_email(self, client, jwt_auth_headers):
        r = client.get("/api/me", headers=jwt_auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert "id" in body
        assert "email" in body
