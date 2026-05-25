"""
Tests for the Phase 3 Slack OAuth surface:
    GET /api/slack/connect-url
    GET /api/slack/oauth/callback
plus the state-signing primitives in slack_oauth.py.
"""

import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from slack_oauth import (
    build_connect_url,
    make_oauth_state,
    verify_oauth_state,
    installation_from_oauth_response,
)


TEST_WS_ID = "00000000-0000-0000-0000-00000000aaaa"
TEST_USER_ID = "00000000-0000-0000-0000-000000000001"


# ── OAuth state — signing + verifying ────────────────────────────────────
class TestOauthState:
    def test_round_trip_valid(self):
        token = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        payload = verify_oauth_state(token)
        assert payload is not None
        assert payload["workspace_id"] == TEST_WS_ID
        assert payload["user_id"]      == TEST_USER_ID
        assert payload["exp"] > int(time.time())

    def test_two_tokens_differ_due_to_nonce(self):
        a = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        b = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        # Same inputs, two distinct tokens — nonce ensures it.
        assert a != b

    def test_empty_string_rejected(self):
        assert verify_oauth_state("") is None

    def test_garbage_string_rejected(self):
        assert verify_oauth_state("not.a.token") is None

    def test_missing_dot_rejected(self):
        assert verify_oauth_state("nodothere") is None

    def test_tampered_signature_rejected(self):
        token = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        head, _ = token.split(".", 1)
        tampered = head + ".AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        assert verify_oauth_state(tampered) is None

    def test_tampered_payload_rejected(self):
        # Different payload re-signed with the right key would pass —
        # but here we mutate the payload portion only, signature stays.
        token = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        head, tail = token.split(".", 1)
        # Flip one char in the payload section.
        head = ("A" if head[0] != "A" else "B") + head[1:]
        tampered = head + "." + tail
        assert verify_oauth_state(tampered) is None

    def test_wrong_secret_rejects_token(self, monkeypatch):
        token = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        monkeypatch.setenv("SLACK_OAUTH_STATE_SECRET", "different-secret")
        assert verify_oauth_state(token) is None

    def test_missing_secret_refuses_to_mint(self, monkeypatch):
        monkeypatch.setenv("SLACK_OAUTH_STATE_SECRET", "")
        with pytest.raises(RuntimeError):
            make_oauth_state(TEST_WS_ID, TEST_USER_ID)

    def test_missing_secret_rejects_verify(self, monkeypatch):
        token = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        monkeypatch.setenv("SLACK_OAUTH_STATE_SECRET", "")
        assert verify_oauth_state(token) is None

    def test_expired_token_rejected(self, monkeypatch):
        # Mint a token, then jump time forward beyond its expiry.
        token = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        # STATE_LIFETIME_SECONDS = 300; jump 1h ahead.
        real_time = time.time
        try:
            time.time = lambda: real_time() + 3600
            assert verify_oauth_state(token) is None
        finally:
            time.time = real_time


# ── Connect URL construction ─────────────────────────────────────────────
class TestBuildConnectUrl:
    def test_url_contains_required_query_params(self):
        url = build_connect_url(
            workspace_id=TEST_WS_ID, user_id=TEST_USER_ID,
        )
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "slack.com"
        assert parsed.path == "/oauth/v2/authorize"

        qs = parse_qs(parsed.query)
        assert "client_id" in qs and qs["client_id"][0]
        assert "scope"        in qs and qs["scope"][0]
        assert "redirect_uri" in qs and qs["redirect_uri"][0]
        assert "state"        in qs and qs["state"][0]

    def test_state_in_url_is_verifiable(self):
        url = build_connect_url(
            workspace_id=TEST_WS_ID, user_id=TEST_USER_ID,
        )
        state = parse_qs(urlparse(url).query)["state"][0]
        payload = verify_oauth_state(state)
        assert payload is not None
        assert payload["workspace_id"] == TEST_WS_ID
        assert payload["user_id"]      == TEST_USER_ID


# ── installation_from_oauth_response ─────────────────────────────────────
class TestProjectInstallation:
    def test_happy_path(self):
        row = installation_from_oauth_response({
            "ok": True,
            "access_token": "xoxb-real-token",
            "scope":        "channels:history,channels:read",
            "bot_user_id":  "U_BOT_1",
            "team":         {"id": "T_TEAM", "name": "Acme"},
        })
        assert row == {
            "slack_team_id":   "T_TEAM",
            "slack_team_name": "Acme",
            "bot_user_id":     "U_BOT_1",
            "bot_token":       "xoxb-real-token",
            "scopes":          "channels:history,channels:read",
        }

    def test_missing_fields_collapse_to_empty(self):
        row = installation_from_oauth_response({})
        assert row["slack_team_id"] == ""
        assert row["bot_token"]     == ""

    def test_handles_missing_team_object(self):
        row = installation_from_oauth_response({"access_token": "xoxb-x"})
        assert row["bot_token"]     == "xoxb-x"
        assert row["slack_team_id"] == ""


# ── GET /api/slack/connect-url ───────────────────────────────────────────
class TestConnectUrlEndpoint:
    def test_returns_slack_url(self, client, jwt_auth_headers):
        r = client.get("/api/slack/connect-url", headers=jwt_auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert "url" in body
        assert body["url"].startswith("https://slack.com/oauth/v2/authorize?")

    def test_requires_auth(self, client):
        r = client.get("/api/slack/connect-url")
        assert r.status_code == 401

    def test_returns_503_when_oauth_disabled(
        self, client, jwt_auth_headers, monkeypatch,
    ):
        monkeypatch.setenv("SLACK_CLIENT_ID", "")
        r = client.get("/api/slack/connect-url", headers=jwt_auth_headers)
        assert r.status_code == 503


# ── GET /api/slack/oauth/callback ────────────────────────────────────────
# The callback doesn't take a JWT — it's hit by Slack's redirect. We
# patch out exchange_code + upsert_slack_installation so the test
# doesn't reach the real Slack API or Supabase.
class TestOauthCallback:
    def test_happy_path_redirects_ok(self, client):
        state = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        fake_resp = {
            "ok":           True,
            "access_token": "xoxb-fresh",
            "scope":        "channels:read",
            "bot_user_id":  "U_BOT",
            "team":         {"id": "T1", "name": "Acme"},
        }
        with patch("main.exchange_code", return_value=fake_resp), \
             patch(
                "main.upsert_slack_installation",
                return_value={"workspace_id": TEST_WS_ID, "slack_team_name": "Acme"},
             ):
            r = client.get(
                "/api/slack/oauth/callback",
                params={"code": "real-code", "state": state},
                follow_redirects=False,
            )
        # 302 redirect to the frontend with slack_connect=ok in the QS.
        assert r.status_code == 302
        loc = r.headers["location"]
        assert "slack_connect=ok" in loc

    def test_user_denied_redirects_error(self, client):
        state = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        r = client.get(
            "/api/slack/oauth/callback",
            params={"error": "access_denied", "state": state},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "slack_connect=error" in r.headers["location"]

    def test_missing_code_redirects_error(self, client):
        state = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        r = client.get(
            "/api/slack/oauth/callback",
            params={"state": state},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "slack_connect=error" in r.headers["location"]

    def test_invalid_state_redirects_error(self, client):
        r = client.get(
            "/api/slack/oauth/callback",
            params={"code": "x", "state": "not-a-real-state"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "slack_connect=error" in r.headers["location"]
        assert "bad_state" in r.headers["location"]

    def test_exchange_failure_redirects_error(self, client):
        state = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        with patch("main.exchange_code", return_value=None):
            r = client.get(
                "/api/slack/oauth/callback",
                params={"code": "real-code", "state": state},
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert "exchange_failed" in r.headers["location"]

    def test_persist_failure_redirects_error(self, client):
        state = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        fake_resp = {
            "ok":           True,
            "access_token": "xoxb-fresh",
            "scope":        "channels:read",
            "bot_user_id":  "U_BOT",
            "team":         {"id": "T1", "name": "Acme"},
        }
        with patch("main.exchange_code", return_value=fake_resp), \
             patch("main.upsert_slack_installation", return_value=None):
            r = client.get(
                "/api/slack/oauth/callback",
                params={"code": "real-code", "state": state},
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert "persist_failed" in r.headers["location"]

    def test_incomplete_install_redirects_error(self, client):
        state = make_oauth_state(TEST_WS_ID, TEST_USER_ID)
        # Slack returned ok but no team id — we shouldn't proceed.
        fake_resp = {
            "ok":           True,
            "access_token": "",
            "team":         {},
        }
        with patch("main.exchange_code", return_value=fake_resp):
            r = client.get(
                "/api/slack/oauth/callback",
                params={"code": "real-code", "state": state},
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert "incomplete_install" in r.headers["location"]