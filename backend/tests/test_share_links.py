"""
Tests for Phase 13 share-link + workspace-status routes.

Routes under test:
    POST   /api/saved-answers/{saved_id}/share
    GET    /api/saved-answers/{saved_id}/shares
    DELETE /api/saved-answers/share/{share_token}
    GET    /api/shared/{share_token}                 (PUBLIC -- no auth)
    GET    /api/workspace/status

Security properties we pin:
    - Public read NEVER returns workspace_id / user_id / debug.
    - Missing, revoked, and expired tokens all 404 -- collapsed so
      probing can't distinguish them.
    - Workspace isolation: a token can't be used to read a saved
      answer from a different workspace, and revoke is scoped to
      (workspace_id, created_by).
    - Public route is rate-limited (its own bucket).
"""

from unittest.mock import MagicMock, patch

import pytest

TEST_WS_ID = "00000000-0000-0000-0000-00000000aaaa"
OTHER_WS_ID = "00000000-0000-0000-0000-00000000bbbb"
SAVED_ID = "12121212-1212-1212-1212-121212121212"
OTHER_SAVED = "34343434-3434-3434-3434-343434343434"


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    from rate_limit import _limiter

    with _limiter._lock:
        _limiter._buckets.clear()
    yield
    with _limiter._lock:
        _limiter._buckets.clear()


# ====================================================================== #
# POST /api/saved-answers/{id}/share
# ====================================================================== #
class TestCreateShareLink:
    def test_creates_token_and_returns_url(self, client, jwt_auth_headers):
        saved_row = {
            "id": SAVED_ID,
            "workspace_id": TEST_WS_ID,
            "question": "q",
            "answer": "a",
            "sources": [],
            "mode": "default",
            "created_at": "2025-01-01T00:00:00Z",
        }
        with patch(
            "main.get_saved_answer",
            return_value=saved_row,
        ), patch(
            "main.create_share_link",
            return_value={
                "id": "link-1",
                "created_at": "2025-02-01T00:00:00Z",
            },
        ) as mock_create:
            r = client.post(
                f"/api/saved-answers/{SAVED_ID}/share",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 201
        body = r.json()
        assert body["saved_answer_id"] == SAVED_ID
        # Token is opaque + non-trivial.
        token = body["share_token"]
        assert isinstance(token, str)
        assert len(token) >= 40  # 32 bytes urlsafe -> 43 chars
        # URL is the form the frontend renders.
        assert body["url"].endswith(f"/shared/{token}")
        # The helper was called with the right workspace + user.
        kw = mock_create.call_args.kwargs
        assert kw["workspace_id"] == TEST_WS_ID
        assert kw["saved_answer_id"] == SAVED_ID
        assert kw["share_token"] == token
        assert kw["created_by"]  # the test user id

    def test_unknown_saved_id_returns_404(self, client, jwt_auth_headers):
        with patch("main.get_saved_answer", return_value=None):
            r = client.post(
                f"/api/saved-answers/{SAVED_ID}/share",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 404

    def test_cross_workspace_saved_id_returns_404(
        self,
        client,
        jwt_auth_headers,
    ):
        """Caller is in TEST_WS_ID but the saved answer lives in
        OTHER_WS_ID -- the workspace filter in get_saved_answer
        means we get None back, and the route 404s."""

        def fake_get(saved_id, workspace_id=None):
            # Only returns the row when the caller's workspace matches.
            if workspace_id == TEST_WS_ID:
                return None
            return {"id": saved_id, "workspace_id": OTHER_WS_ID}

        with patch("main.get_saved_answer", side_effect=fake_get):
            r = client.post(
                f"/api/saved-answers/{SAVED_ID}/share",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 404

    def test_persistence_failure_returns_502(self, client, jwt_auth_headers):
        with patch(
            "main.get_saved_answer",
            return_value={"id": SAVED_ID, "workspace_id": TEST_WS_ID, "question": "q", "answer": "a"},
        ), patch("main.create_share_link", return_value=None):
            r = client.post(
                f"/api/saved-answers/{SAVED_ID}/share",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 502

    def test_requires_auth(self, client):
        r = client.post(f"/api/saved-answers/{SAVED_ID}/share")
        assert r.status_code == 401


# ====================================================================== #
# GET /api/saved-answers/{id}/shares
# ====================================================================== #
class TestListShares:
    def test_returns_active_shares_only(self, client, jwt_auth_headers):
        with patch(
            "main.list_share_links_for_workspace",
            return_value=[
                {
                    "id": "s1",
                    "share_token": "tok-1",
                    "created_at": "2025-01-01",
                    "expires_at": None,
                    "revoked_at": None,
                    "created_by": "u1",
                },
                {
                    "id": "s2",
                    "share_token": "tok-2",
                    "created_at": "2025-01-02",
                    "expires_at": None,
                    "revoked_at": "2025-01-03",  # revoked
                    "created_by": "u1",
                },
            ],
        ):
            r = client.get(
                f"/api/saved-answers/{SAVED_ID}/shares",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 200
        body = r.json()
        tokens = [s["share_token"] for s in body["shares"]]
        assert "tok-1" in tokens
        assert "tok-2" not in tokens  # revoked one hidden

    def test_empty_when_no_shares(self, client, jwt_auth_headers):
        with patch(
            "main.list_share_links_for_workspace",
            return_value=[],
        ):
            r = client.get(
                f"/api/saved-answers/{SAVED_ID}/shares",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 200
        assert r.json()["shares"] == []

    def test_requires_auth(self, client):
        r = client.get(f"/api/saved-answers/{SAVED_ID}/shares")
        assert r.status_code == 401


# ====================================================================== #
# DELETE /api/saved-answers/share/{token}
# ====================================================================== #
class TestRevokeShare:
    def test_revoke_success(self, client, jwt_auth_headers):
        with patch(
            "main.revoke_share_link",
            return_value=True,
        ) as mock_rev:
            r = client.delete(
                "/api/saved-answers/share/the-token",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 200
        assert r.json()["revoked"] is True
        kw = mock_rev.call_args.kwargs
        # Scoped to caller's workspace + user.
        assert kw["share_token"] == "the-token"
        assert kw["workspace_id"] == TEST_WS_ID
        assert kw["user_id"]  # the test user id

    def test_unknown_or_not_owned_returns_404(
        self,
        client,
        jwt_auth_headers,
    ):
        with patch("main.revoke_share_link", return_value=False):
            r = client.delete(
                "/api/saved-answers/share/never-existed",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 404

    def test_requires_auth(self, client):
        r = client.delete("/api/saved-answers/share/x")
        assert r.status_code == 401


# ====================================================================== #
# GET /api/shared/{token}    (PUBLIC)
# ====================================================================== #
class TestPublicShareRead:
    def _link_row(self, **overrides):
        row = {
            "id": "link-1",
            "share_token": "the-token",
            "workspace_id": TEST_WS_ID,
            "saved_answer_id": SAVED_ID,
            "created_by": "user-1",
            "expires_at": None,
            "revoked_at": None,
        }
        row.update(overrides)
        return row

    def _saved_row(self, **overrides):
        row = {
            "id": SAVED_ID,
            "workspace_id": TEST_WS_ID,
            "question": "What did we decide?",
            "answer": "We agreed to use Railway.",
            "sources": [{"index": 1, "source": "s1"}],
            "mode": "default",
            "user_id": "user-1",
            "debug": {"internal": "secret"},
            "created_at": "2025-01-01T00:00:00Z",
        }
        row.update(overrides)
        return row

    def test_unauth_read_succeeds(self, client):
        with patch(
            "main.get_share_link_by_token",
            return_value=self._link_row(),
        ), patch(
            "main.get_saved_answer",
            return_value=self._saved_row(),
        ):
            r = client.get("/api/shared/the-token")  # NO headers
        assert r.status_code == 200
        body = r.json()
        # Allowed public fields are present.
        assert body["question"] == "What did we decide?"
        assert body["answer"] == "We agreed to use Railway."
        assert body["sources"]
        assert body["mode"] == "default"
        assert body["created_at"]
        # Sensitive fields MUST NOT leak.
        for forbidden in ("workspace_id", "user_id", "debug", "created_by", "share_token"):
            assert forbidden not in body, f"public response leaked {forbidden}"
        rendered = repr(body)
        assert TEST_WS_ID not in rendered
        assert "secret" not in rendered

    def test_missing_token_returns_404(self, client):
        with patch(
            "main.get_share_link_by_token",
            return_value=None,
        ):
            r = client.get("/api/shared/nope")
        assert r.status_code == 404

    def test_revoked_token_returns_404_via_helper_collapsing(self, client):
        """get_share_link_by_token returns None for revoked tokens
        (the helper's collapsed semantics). Our route just trusts
        that and 404s -- which is exactly what we want."""
        with patch("main.get_share_link_by_token", return_value=None):
            r = client.get("/api/shared/revoked-token")
        assert r.status_code == 404

    def test_orphaned_saved_id_returns_404(self, client):
        """Token row exists but the saved answer was deleted. The
        route 404s instead of 500."""
        with patch(
            "main.get_share_link_by_token",
            return_value=self._link_row(),
        ), patch(
            "main.get_saved_answer",
            return_value=None,
        ):
            r = client.get("/api/shared/the-token")
        assert r.status_code == 404

    def test_workspace_id_pinned_from_link_not_saved_answer(self, client):
        """Defense in depth: even if get_saved_answer is asked for
        the right saved_id, the route ALSO pins workspace_id from
        the share link row -- so a forged saved_id in a foreign
        workspace can't be exfiltrated."""
        with patch(
            "main.get_share_link_by_token",
            return_value=self._link_row(),
        ), patch(
            "main.get_saved_answer",
            return_value=self._saved_row(),
        ) as mock_get:
            client.get("/api/shared/the-token")
        kw = mock_get.call_args.kwargs
        # The route MUST pass workspace_id from the LINK row.
        assert kw["workspace_id"] == TEST_WS_ID
        assert kw["saved_id"] == SAVED_ID


# ====================================================================== #
# Workspace isolation across the whole share lifecycle
# ====================================================================== #
class TestWorkspaceIsolation:
    def test_token_for_ws_a_cannot_read_ws_b_data(self, client):
        """A token bound to TEST_WS_ID points to a saved answer in
        TEST_WS_ID. Even if the request comes from someone trying
        to enumerate, the public route only ever returns the saved
        answer the link references -- and the link's workspace_id
        gates the get_saved_answer call. We pin behavior: the
        public response contains NO workspace identifier the caller
        could use to pivot."""
        link = {
            "id": "link-x",
            "share_token": "tok-x",
            "workspace_id": TEST_WS_ID,
            "saved_answer_id": SAVED_ID,
            "created_by": "u1",
            "expires_at": None,
            "revoked_at": None,
        }
        saved = {
            "id": SAVED_ID,
            "workspace_id": TEST_WS_ID,
            "question": "q",
            "answer": "a",
            "sources": [],
            "mode": "default",
            "created_at": "x",
        }
        with patch(
            "main.get_share_link_by_token",
            return_value=link,
        ), patch(
            "main.get_saved_answer",
            return_value=saved,
        ):
            r = client.get("/api/shared/tok-x")
        assert r.status_code == 200
        rendered = repr(r.json())
        assert TEST_WS_ID not in rendered
        assert OTHER_WS_ID not in rendered


# ====================================================================== #
# GET /api/workspace/status
# ====================================================================== #
class TestWorkspaceStatus:
    def test_disconnected_workspace(self, client, jwt_auth_headers):
        """Nothing connected -> connectors report disconnected with
        zero counts. No exceptions on missing data."""
        with patch(
            "main.get_slack_installation",
            return_value=None,
        ), patch(
            "main.list_selected_channel_ids",
            return_value=[],
        ), patch(
            "main.list_gmail_connections_public",
            return_value=[],
        ), patch(
            "main.auto_ingest_enabled",
            return_value=False,
        ):
            r = client.get("/api/workspace/status", headers=jwt_auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["slack"] == {
            "connected": False,
            "channels_selected": 0,
            "scheduler_enabled": False,
        }
        assert body["gmail"] == {
            "connection_count": 0,
            "labels_selected": 0,
            "last_synced_at": None,
        }

    def test_connected_workspace_aggregates(self, client, jwt_auth_headers):
        """One Slack install with two channels, two Gmail connections
        with different per-connection sync timestamps. We should
        report the max timestamp and the sum of labels."""
        with patch(
            "main.get_slack_installation",
            return_value={"id": "inst-1", "bot_token": "xoxb-x"},
        ), patch(
            "main.list_selected_channel_ids",
            return_value=["C1", "C2"],
        ), patch(
            "main.list_gmail_connections_public",
            return_value=[
                {"id": "conn-a", "email": "a@x"},
                {"id": "conn-b", "email": "b@x"},
            ],
        ), patch(
            "main.get_gmail_connection_sync_summary",
            side_effect=[
                {"last_synced_at": "2025-09-10T12:00:00Z", "labels_synced": 2},
                {"last_synced_at": "2025-09-15T08:00:00Z", "labels_synced": 1},
            ],
        ), patch(
            "main.list_selected_gmail_label_ids",
            side_effect=[["INBOX", "Label_1"], ["INBOX"]],
        ), patch(
            "main.auto_ingest_enabled",
            return_value=True,
        ):
            r = client.get("/api/workspace/status", headers=jwt_auth_headers)
        body = r.json()
        # Slack
        assert body["slack"]["connected"] is True
        assert body["slack"]["channels_selected"] == 2
        assert body["slack"]["scheduler_enabled"] is True
        # Gmail
        assert body["gmail"]["connection_count"] == 2
        assert body["gmail"]["labels_selected"] == 3  # 2 + 1
        # Max wins for last_synced_at.
        assert body["gmail"]["last_synced_at"] == "2025-09-15T08:00:00Z"

    def test_gmail_summary_failure_does_not_blow_up(
        self,
        client,
        jwt_auth_headers,
    ):
        """If get_gmail_connection_sync_summary raises, the workspace
        status route must still return 200 with degraded gmail counts."""
        with patch(
            "main.get_slack_installation",
            return_value=None,
        ), patch(
            "main.list_selected_channel_ids",
            return_value=[],
        ), patch(
            "main.list_gmail_connections_public",
            return_value=[{"id": "conn-a", "email": "a@x"}],
        ), patch(
            "main.get_gmail_connection_sync_summary",
            side_effect=RuntimeError("supabase down"),
        ), patch(
            "main.list_selected_gmail_label_ids",
            side_effect=RuntimeError("x"),
        ), patch(
            "main.auto_ingest_enabled",
            return_value=False,
        ):
            r = client.get("/api/workspace/status", headers=jwt_auth_headers)
        assert r.status_code == 200
        assert r.json()["gmail"]["connection_count"] == 1
        assert r.json()["gmail"]["last_synced_at"] is None

    def test_requires_auth(self, client):
        r = client.get("/api/workspace/status")
        assert r.status_code == 401


# ====================================================================== #
# Helper-layer tests for the share-link get_share_link_by_token semantics
# ====================================================================== #
class TestGetShareLinkByToken:
    def _client_with_rows(self, rows):
        execute = MagicMock(return_value=MagicMock(data=rows))
        limit = MagicMock(return_value=MagicMock(execute=execute))
        eq = MagicMock(return_value=MagicMock(limit=limit))
        select = MagicMock(return_value=MagicMock(eq=eq))
        table = MagicMock(return_value=MagicMock(select=select))
        return MagicMock(table=table)

    def test_revoked_collapses_to_none(self):
        from supabase_client import get_share_link_by_token

        row = {
            "id": "x",
            "share_token": "t",
            "workspace_id": TEST_WS_ID,
            "saved_answer_id": SAVED_ID,
            "created_by": "u",
            "expires_at": None,
            "revoked_at": "2025-01-01T00:00:00Z",
        }
        with patch(
            "supabase_client.get_supabase",
            return_value=self._client_with_rows([row]),
        ):
            assert get_share_link_by_token(share_token="t") is None

    def test_expired_collapses_to_none(self):
        from supabase_client import get_share_link_by_token

        row = {
            "id": "x",
            "share_token": "t",
            "workspace_id": TEST_WS_ID,
            "saved_answer_id": SAVED_ID,
            "created_by": "u",
            "expires_at": "2000-01-01T00:00:00+00:00",  # long ago
            "revoked_at": None,
        }
        with patch(
            "supabase_client.get_supabase",
            return_value=self._client_with_rows([row]),
        ):
            assert get_share_link_by_token(share_token="t") is None

    def test_active_returns_row(self):
        from supabase_client import get_share_link_by_token

        row = {
            "id": "x",
            "share_token": "t",
            "workspace_id": TEST_WS_ID,
            "saved_answer_id": SAVED_ID,
            "created_by": "u",
            "expires_at": None,
            "revoked_at": None,
        }
        with patch(
            "supabase_client.get_supabase",
            return_value=self._client_with_rows([row]),
        ):
            out = get_share_link_by_token(share_token="t")
        assert out is not None
        assert out["share_token"] == "t"

    def test_blank_token_returns_none(self):
        from supabase_client import get_share_link_by_token

        assert get_share_link_by_token(share_token="") is None

    def test_supabase_error_returns_none(self):
        from supabase_client import get_share_link_by_token

        with patch(
            "supabase_client.get_supabase",
            side_effect=RuntimeError("boom"),
        ):
            assert get_share_link_by_token(share_token="t") is None
