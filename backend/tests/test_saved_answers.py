"""
Tests for the Phase 2 saved-answers routes.

Same pattern as test_chat_history.py — we patch the supabase_client
helpers in main's namespace and assert the HTTP contract.
"""

from unittest.mock import patch

TEST_WS_ID = "00000000-0000-0000-0000-00000000aaaa"


# ── GET /api/saved-answers ────────────────────────────────────────────────
class TestListSavedAnswers:
    def test_returns_saved_answers(self, client, jwt_auth_headers):
        rows = [
            {
                "id": "saved-1",
                "question": "Deadline?",
                "answer": "Friday.",
                "sources": [],
                "mode": "default",
                "filters": {"topK": 5},
                "debug": None,
                "created_at": "2026-01-02T11:00:00+00:00",
            },
            {
                "id": "saved-2",
                "question": "Who owns auth?",
                "answer": "Alice.",
                "sources": [{"title": "doc"}],
                "mode": "who_said",
                "filters": {"topK": 3, "channel": "eng"},
                "debug": {"cache_hit": False},
                "created_at": "2026-01-03T09:00:00+00:00",
            },
        ]
        with patch("main.list_saved_answers", return_value=rows) as mock_fn:
            r = client.get("/api/saved-answers", headers=jwt_auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert len(body) == 2
        assert body[0]["question"] == "Deadline?"
        _, kwargs = mock_fn.call_args
        assert kwargs["workspace_id"] == TEST_WS_ID

    def test_empty_when_none(self, client, jwt_auth_headers):
        with patch("main.list_saved_answers", return_value=[]):
            r = client.get("/api/saved-answers", headers=jwt_auth_headers)
        assert r.status_code == 200
        assert r.json() == []

    def test_requires_auth(self, client):
        r = client.get("/api/saved-answers")
        assert r.status_code == 401


# ── POST /api/saved-answers ───────────────────────────────────────────────
class TestCreateSavedAnswer:
    def test_creates_saved_answer(self, client, jwt_auth_headers):
        created = {
            "id": "saved-new",
            "question": "Q",
            "answer": "A",
            "sources": [],
            "mode": "default",
            "filters": {"topK": 5},
            "debug": None,
            "created_at": "2026-01-04T10:00:00+00:00",
        }
        payload = {
            "question": "Q",
            "answer": "A",
            "sources": [],
            "mode": "default",
            "filters": {"topK": 5},
        }
        with patch("main.create_saved_answer", return_value=created) as mock_fn:
            r = client.post(
                "/api/saved-answers",
                headers=jwt_auth_headers,
                json=payload,
            )
        assert r.status_code == 201
        body = r.json()
        assert body["id"] == "saved-new"
        _, kwargs = mock_fn.call_args
        assert kwargs["question"] == "Q"
        assert kwargs["answer"] == "A"
        assert kwargs["mode"] == "default"
        assert kwargs["filters"] == {"topK": 5}
        assert kwargs["workspace_id"] == TEST_WS_ID

    def test_accepts_minimal_payload(self, client, jwt_auth_headers):
        created = {
            "id": "saved-min",
            "question": "",
            "answer": "",
            "sources": None,
            "mode": None,
            "filters": None,
            "debug": None,
            "created_at": "2026-01-04T10:00:00+00:00",
        }
        with patch("main.create_saved_answer", return_value=created):
            r = client.post(
                "/api/saved-answers",
                headers=jwt_auth_headers,
                json={},
            )
        assert r.status_code == 201

    def test_rejects_overlong_question(self, client, jwt_auth_headers):
        r = client.post(
            "/api/saved-answers",
            headers=jwt_auth_headers,
            json={"question": "x" * 5001, "answer": "ok"},
        )
        assert r.status_code == 422

    def test_rejects_extra_field(self, client, jwt_auth_headers):
        r = client.post(
            "/api/saved-answers",
            headers=jwt_auth_headers,
            json={"question": "Q", "answer": "A", "random": "nope"},
        )
        assert r.status_code == 422

    def test_db_failure_returns_502(self, client, jwt_auth_headers):
        with patch("main.create_saved_answer", return_value=None):
            r = client.post(
                "/api/saved-answers",
                headers=jwt_auth_headers,
                json={"question": "Q", "answer": "A"},
            )
        assert r.status_code == 502

    def test_requires_auth(self, client):
        r = client.post(
            "/api/saved-answers",
            json={"question": "Q", "answer": "A"},
        )
        assert r.status_code == 401


# ── DELETE /api/saved-answers/{id} ────────────────────────────────────────
class TestDeleteSavedAnswer:
    def test_deletes_existing_saved_answer(self, client, jwt_auth_headers):
        with patch("main.delete_saved_answer", return_value=True) as mock_fn:
            r = client.delete(
                "/api/saved-answers/saved-1",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 200
        body = r.json()
        assert body == {"id": "saved-1", "deleted": True}
        _, kwargs = mock_fn.call_args
        assert kwargs["saved_id"] == "saved-1"
        assert kwargs["workspace_id"] == TEST_WS_ID

    def test_unknown_id_returns_404(self, client, jwt_auth_headers):
        with patch("main.delete_saved_answer", return_value=False):
            r = client.delete(
                "/api/saved-answers/unknown",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 404

    def test_requires_auth(self, client):
        r = client.delete("/api/saved-answers/saved-1")
        assert r.status_code == 401
