"""
Tests for the Phase 3 Slack channel + ingest routes:
    GET  /api/slack/channels
    POST /api/slack/channels
    POST /api/slack/ingest

We patch the supabase_client helpers and the Slack-API enumeration at
the main module's namespace (main.list_slack_channels, etc.) — the
routes call them by name from main, so the patch sits exactly there.
"""

from unittest.mock import patch

import pytest


TEST_WS_ID = "00000000-0000-0000-0000-00000000aaaa"


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """
    Phase 7: the new per-bucket rate limits (slack_ingest=5/5min,
    auth=30/5min, etc.) accumulate across tests in the same process.
    Without resetting them, the 6th test in TestRunIngest hits the
    /api/slack/ingest limit and gets 429 -- shadowing the 401/400/etc.
    the test was actually checking for. Clear the buckets per-test
    so each assertion sees the correct status code.
    """
    from rate_limit import _limiter
    with _limiter._lock:
        _limiter._buckets.clear()
    yield
    with _limiter._lock:
        _limiter._buckets.clear()



# ── GET /api/slack/channels ──────────────────────────────────────────────
class TestListChannels:
    def test_not_connected_returns_empty_state(self, client, jwt_auth_headers):
        with patch("main.get_slack_installation", return_value=None):
            r = client.get("/api/slack/channels", headers=jwt_auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is False
        assert body["channels"] == []

    def test_connected_returns_channels_and_refreshes(
        self, client, jwt_auth_headers,
    ):
        install = {
            "workspace_id":    TEST_WS_ID,
            "slack_team_name": "Acme",
            "bot_token":       "xoxb-test",
        }
        fresh_from_slack = [
            {"slack_channel_id": "C1", "name": "general", "is_archived": False},
            {"slack_channel_id": "C2", "name": "random",  "is_archived": False},
        ]
        stored_rows = [
            {
                "slack_channel_id": "C1",
                "name":             "general",
                "is_selected":      True,
                "is_archived":      False,
                "updated_at":       "2026-01-01T10:00:00+00:00",
            },
            {
                "slack_channel_id": "C2",
                "name":             "random",
                "is_selected":      False,
                "is_archived":      False,
                "updated_at":       "2026-01-01T10:00:00+00:00",
            },
        ]
        with patch("main.get_slack_installation", return_value=install), \
             patch(
                "main.list_slack_channels", return_value=fresh_from_slack,
             ) as mock_fresh, \
             patch(
                "main.upsert_slack_channels", return_value=2,
             ) as mock_upsert, \
             patch(
                "main.list_workspace_channels", return_value=stored_rows,
             ) as mock_list:
            r = client.get("/api/slack/channels", headers=jwt_auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is True
        assert body["team_name"] == "Acme"
        assert len(body["channels"]) == 2

        # Verify the refresh-then-read pattern actually fired.
        mock_fresh.assert_called_once()
        mock_upsert.assert_called_once()
        _, kwargs = mock_list.call_args
        assert kwargs["workspace_id"] == TEST_WS_ID

    def test_connected_but_slack_refresh_fails_still_returns_stored(
        self, client, jwt_auth_headers,
    ):
        # Slack API hiccup shouldn't break the picker — we just return
        # what's already in the DB.
        install = {"slack_team_name": "Acme", "bot_token": "xoxb-test"}
        with patch("main.get_slack_installation", return_value=install), \
             patch("main.list_slack_channels", return_value=[]), \
             patch(
                "main.upsert_slack_channels", return_value=0,
             ) as mock_upsert, \
             patch(
                "main.list_workspace_channels",
                return_value=[{
                    "slack_channel_id": "C1", "name": "general",
                    "is_selected": True, "is_archived": False,
                    "updated_at": "2026-01-01T10:00:00+00:00",
                }],
             ):
            r = client.get("/api/slack/channels", headers=jwt_auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["connected"] is True
        # No fresh channels -> no upsert call.
        mock_upsert.assert_not_called()
        assert len(body["channels"]) == 1

    def test_requires_auth(self, client):
        r = client.get("/api/slack/channels")
        assert r.status_code == 401


# ── POST /api/slack/channels ─────────────────────────────────────────────
class TestSaveChannels:
    def test_saves_selected_set(self, client, jwt_auth_headers):
        with patch(
            "main.set_selected_channels", return_value=True,
        ) as mock_fn:
            r = client.post(
                "/api/slack/channels",
                headers=jwt_auth_headers,
                json={"selected_channel_ids": ["C1", "C3"]},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["selected_count"] == 2
        _, kwargs = mock_fn.call_args
        assert kwargs["workspace_id"]  == TEST_WS_ID
        assert kwargs["selected_ids"]  == ["C1", "C3"]

    def test_saves_empty_set(self, client, jwt_auth_headers):
        with patch(
            "main.set_selected_channels", return_value=True,
        ) as mock_fn:
            r = client.post(
                "/api/slack/channels",
                headers=jwt_auth_headers,
                json={"selected_channel_ids": []},
            )
        assert r.status_code == 200
        assert r.json()["selected_count"] == 0
        _, kwargs = mock_fn.call_args
        assert kwargs["selected_ids"] == []

    def test_db_failure_returns_502(self, client, jwt_auth_headers):
        with patch("main.set_selected_channels", return_value=False):
            r = client.post(
                "/api/slack/channels",
                headers=jwt_auth_headers,
                json={"selected_channel_ids": ["C1"]},
            )
        assert r.status_code == 502

    def test_rejects_extra_field(self, client, jwt_auth_headers):
        r = client.post(
            "/api/slack/channels",
            headers=jwt_auth_headers,
            json={"selected_channel_ids": ["C1"], "random": "no"},
        )
        assert r.status_code == 422

    def test_requires_auth(self, client):
        r = client.post(
            "/api/slack/channels",
            json={"selected_channel_ids": ["C1"]},
        )
        assert r.status_code == 401


# ── POST /api/slack/ingest ───────────────────────────────────────────────
class TestRunIngest:
    def test_kicks_off_background_ingest(self, client, jwt_auth_headers):
        install = {"bot_token": "xoxb-test"}
        with patch(
            "main.get_slack_installation", return_value=install,
        ), patch(
            "main.list_selected_channel_ids", return_value=["C1", "C2"],
        ), patch(
            # Phase 4: the route resolves the workspace's HydraDB
            # sub-tenant before scheduling the runner. Patch this so
            # the test doesn't hit Supabase.
            "main.ensure_workspace_sub_tenant", return_value="ws_test_abc",
        ), patch(
            "main.run_workspace_ingest", return_value={},
        ) as mock_runner:
            r = client.post("/api/slack/ingest", headers=jwt_auth_headers)
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "started"
        assert body["channels_queued"] == 2
        # BackgroundTask executes synchronously when TestClient drains
        # its lifespan, so the runner WILL have been called.
        mock_runner.assert_called_once()
        _, kwargs = mock_runner.call_args
        assert kwargs["workspace_id"]          == TEST_WS_ID
        assert kwargs["bot_token"]             == "xoxb-test"
        assert kwargs["channel_ids"]           == ["C1", "C2"]
        # Phase 4: the resolved sub_tenant_id must be forwarded.
        assert kwargs["hydradb_sub_tenant_id"] == "ws_test_abc"

    def test_no_installation_returns_400(self, client, jwt_auth_headers):
        with patch("main.get_slack_installation", return_value=None):
            r = client.post("/api/slack/ingest", headers=jwt_auth_headers)
        assert r.status_code == 400

    def test_blank_bot_token_returns_400(self, client, jwt_auth_headers):
        with patch(
            "main.get_slack_installation",
            return_value={"bot_token": "   "},
        ):
            r = client.post("/api/slack/ingest", headers=jwt_auth_headers)
        assert r.status_code == 400

    def test_no_channels_selected_returns_400(self, client, jwt_auth_headers):
        with patch(
            "main.get_slack_installation",
            return_value={"bot_token": "xoxb-test"},
        ), patch(
            "main.list_selected_channel_ids", return_value=[],
        ):
            r = client.post("/api/slack/ingest", headers=jwt_auth_headers)
        assert r.status_code == 400

    def test_sub_tenant_lookup_failure_returns_502(
        self, client, jwt_auth_headers,
    ):
        # Phase 4: if ensure_workspace_sub_tenant returns None we
        # refuse rather than fall back to the env default -- that
        # would leak data into the shared HydraDB bucket.
        with patch(
            "main.get_slack_installation",
            return_value={"bot_token": "xoxb-test"},
        ), patch(
            "main.list_selected_channel_ids", return_value=["C1"],
        ), patch(
            "main.ensure_workspace_sub_tenant", return_value=None,
        ), patch(
            "main.run_workspace_ingest",
        ) as mock_runner:
            r = client.post("/api/slack/ingest", headers=jwt_auth_headers)
        assert r.status_code == 502
        mock_runner.assert_not_called()

    def test_requires_auth(self, client):
        r = client.post("/api/slack/ingest")
        assert r.status_code == 401