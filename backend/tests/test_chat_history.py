"""
Tests for the Phase 2 chat-history routes.

We patch the supabase_client helpers at the module-import boundary
(main.list_chat_sessions, etc.) — the routes call them by name from the
main namespace because of the `from supabase_client import ...` line.
That's the same pattern test_workspace_resolution.py uses for
list_user_workspaces.
"""

from unittest.mock import patch


TEST_WS_ID = "00000000-0000-0000-0000-00000000aaaa"


# ── GET /api/chat/sessions ────────────────────────────────────────────────
class TestListSessions:
    def test_returns_sessions_list(self, client, jwt_auth_headers):
        rows = [
            {
                "id":         "sess-1",
                "title":      "First chat",
                "created_at": "2026-01-01T10:00:00+00:00",
                "updated_at": "2026-01-01T10:05:00+00:00",
            },
            {
                "id":         "sess-2",
                "title":      "Second chat",
                "created_at": "2026-01-02T11:00:00+00:00",
                "updated_at": "2026-01-02T11:00:00+00:00",
            },
        ]
        with patch("main.list_chat_sessions", return_value=rows) as mock_fn:
            r = client.get("/api/chat/sessions", headers=jwt_auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert len(body) == 2
        assert body[0]["id"] == "sess-1"
        _, kwargs = mock_fn.call_args
        assert kwargs["workspace_id"] == TEST_WS_ID
        assert kwargs["user_id"]

    def test_empty_when_no_sessions(self, client, jwt_auth_headers):
        with patch("main.list_chat_sessions", return_value=[]):
            r = client.get("/api/chat/sessions", headers=jwt_auth_headers)
        assert r.status_code == 200
        assert r.json() == []

    def test_requires_auth(self, client):
        r = client.get("/api/chat/sessions")
        assert r.status_code == 401


# ── POST /api/chat/sessions ───────────────────────────────────────────────
class TestCreateSession:
    def test_creates_session_with_title(self, client, jwt_auth_headers):
        created_row = {
            "id":         "sess-new",
            "title":      "About auth",
            "created_at": "2026-01-03T09:00:00+00:00",
            "updated_at": "2026-01-03T09:00:00+00:00",
        }
        with patch(
            "main.create_chat_session", return_value=created_row
        ) as mock_fn:
            r = client.post(
                "/api/chat/sessions",
                headers=jwt_auth_headers,
                json={"title": "About auth"},
            )
        assert r.status_code == 201
        body = r.json()
        assert body["id"] == "sess-new"
        assert body["title"] == "About auth"
        _, kwargs = mock_fn.call_args
        assert kwargs["title"] == "About auth"
        assert kwargs["workspace_id"] == TEST_WS_ID

    def test_title_defaults_to_new_chat(self, client, jwt_auth_headers):
        created_row = {
            "id":    "sess-new",
            "title": "New chat",
        }
        with patch(
            "main.create_chat_session", return_value=created_row
        ) as mock_fn:
            r = client.post(
                "/api/chat/sessions",
                headers=jwt_auth_headers,
                json={},
            )
        assert r.status_code == 201
        _, kwargs = mock_fn.call_args
        assert kwargs["title"] == "New chat"

    def test_db_failure_returns_502(self, client, jwt_auth_headers):
        with patch("main.create_chat_session", return_value=None):
            r = client.post(
                "/api/chat/sessions",
                headers=jwt_auth_headers,
                json={"title": "x"},
            )
        assert r.status_code == 502

    def test_rejects_overlong_title(self, client, jwt_auth_headers):
        r = client.post(
            "/api/chat/sessions",
            headers=jwt_auth_headers,
            json={"title": "x" * 201},
        )
        assert r.status_code == 422

    def test_rejects_extra_field(self, client, jwt_auth_headers):
        r = client.post(
            "/api/chat/sessions",
            headers=jwt_auth_headers,
            json={"title": "ok", "random": "nope"},
        )
        assert r.status_code == 422

    def test_requires_auth(self, client):
        r = client.post("/api/chat/sessions", json={"title": "x"})
        assert r.status_code == 401


# ── GET /api/chat/sessions/{id}/messages ──────────────────────────────────
class TestListMessages:
    def test_returns_messages_oldest_first(self, client, jwt_auth_headers):
        rows = [
            {
                "id":         "m1",
                "role":       "user",
                "content":    "What did we ship?",
                "sources":    None,
                "created_at": "2026-01-01T10:00:00+00:00",
            },
            {
                "id":         "m2",
                "role":       "assistant",
                "content":    "The auth migration.",
                "sources":    [],
                "created_at": "2026-01-01T10:00:05+00:00",
            },
        ]
        with patch(
            "main.list_chat_messages", return_value=rows
        ) as mock_fn:
            r = client.get(
                "/api/chat/sessions/sess-1/messages",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 2
        assert body[0]["role"] == "user"
        assert body[1]["role"] == "assistant"
        _, kwargs = mock_fn.call_args
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["workspace_id"] == TEST_WS_ID

    def test_unknown_session_returns_empty_list(self, client, jwt_auth_headers):
        with patch("main.list_chat_messages", return_value=[]):
            r = client.get(
                "/api/chat/sessions/unknown/messages",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 200
        assert r.json() == []

    def test_requires_auth(self, client):
        r = client.get("/api/chat/sessions/sess-1/messages")
        assert r.status_code == 401


# ── POST /api/chat/sessions/{id}/messages ─────────────────────────────────
class TestCreateMessage:
    def test_appends_user_message(self, client, jwt_auth_headers):
        created = {
            "id":         "msg-1",
            "role":       "user",
            "content":    "Hi",
            "sources":    None,
            "created_at": "2026-01-01T10:00:00+00:00",
        }
        with patch(
            "main.create_chat_message", return_value=created
        ) as mock_fn:
            r = client.post(
                "/api/chat/sessions/sess-1/messages",
                headers=jwt_auth_headers,
                json={"role": "user", "content": "Hi"},
            )
        assert r.status_code == 201
        body = r.json()
        assert body["id"] == "msg-1"
        _, kwargs = mock_fn.call_args
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["role"] == "user"
        assert kwargs["content"] == "Hi"
        assert kwargs["sources"] is None

    def test_appends_assistant_message_with_sources(
        self, client, jwt_auth_headers,
    ):
        srcs = [{"title": "doc1", "url": "https://example.com/1"}]
        created = {
            "id":         "msg-2",
            "role":       "assistant",
            "content":    "Answer.",
            "sources":    srcs,
            "created_at": "2026-01-01T10:00:05+00:00",
        }
        with patch(
            "main.create_chat_message", return_value=created
        ) as mock_fn:
            r = client.post(
                "/api/chat/sessions/sess-1/messages",
                headers=jwt_auth_headers,
                json={
                    "role":    "assistant",
                    "content": "Answer.",
                    "sources": srcs,
                },
            )
        assert r.status_code == 201
        _, kwargs = mock_fn.call_args
        assert kwargs["sources"] == srcs

    def test_rejects_invalid_role(self, client, jwt_auth_headers):
        r = client.post(
            "/api/chat/sessions/sess-1/messages",
            headers=jwt_auth_headers,
            json={"role": "system", "content": "evil"},
        )
        assert r.status_code == 422

    def test_db_failure_returns_400(self, client, jwt_auth_headers):
        with patch("main.create_chat_message", return_value=None):
            r = client.post(
                "/api/chat/sessions/sess-stolen/messages",
                headers=jwt_auth_headers,
                json={"role": "user", "content": "Hi"},
            )
        assert r.status_code == 400

    def test_requires_auth(self, client):
        r = client.post(
            "/api/chat/sessions/sess-1/messages",
            json={"role": "user", "content": "Hi"},
        )
        assert r.status_code == 401