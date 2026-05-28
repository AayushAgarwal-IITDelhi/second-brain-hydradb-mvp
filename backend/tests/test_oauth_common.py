"""
Tests for oauth_common — the shared HMAC-signed OAuth state helpers.

These tests exercise the underlying crypto directly (passing an explicit
secret argument) rather than going through the connector wrappers.
Connector-specific behaviour (secret sourced from env, RuntimeError on
missing secret) is covered by test_slack_oauth.py and test_gmail_oauth.py.
"""

import time

import pytest

from oauth_common import make_oauth_state, verify_oauth_state

SECRET = "test-secret-key-not-for-production"
WS_ID = "00000000-0000-0000-0000-000000000001"
USER_ID = "00000000-0000-0000-0000-000000000002"


# ── Round-trip / happy path ───────────────────────────────────────────────

class TestMakeAndVerify:
    def test_round_trip(self):
        token = make_oauth_state(SECRET, WS_ID, USER_ID)
        payload = verify_oauth_state(SECRET, token)

        assert payload is not None
        assert payload["workspace_id"] == WS_ID
        assert payload["user_id"] == USER_ID
        assert payload["exp"] > int(time.time())
        assert "nonce" in payload

    def test_nonce_makes_tokens_unique(self):
        a = make_oauth_state(SECRET, WS_ID, USER_ID)
        b = make_oauth_state(SECRET, WS_ID, USER_ID)
        assert a != b

    def test_custom_lifetime(self):
        token = make_oauth_state(SECRET, WS_ID, USER_ID, lifetime_seconds=60)
        payload = verify_oauth_state(SECRET, token)
        assert payload is not None
        # exp should be ~60 s in the future, not the default 300.
        assert payload["exp"] <= int(time.time()) + 61


# ── verify_oauth_state — rejection cases ────────────────────────────────

class TestVerifyRejects:
    def test_empty_state(self):
        assert verify_oauth_state(SECRET, "") is None

    def test_no_dot_separator(self):
        assert verify_oauth_state(SECRET, "nodothere") is None

    def test_garbage_string(self):
        assert verify_oauth_state(SECRET, "not.a.real.token") is None

    def test_empty_secret(self):
        token = make_oauth_state(SECRET, WS_ID, USER_ID)
        assert verify_oauth_state("", token) is None

    def test_wrong_secret(self):
        token = make_oauth_state(SECRET, WS_ID, USER_ID)
        assert verify_oauth_state("wrong-secret", token) is None

    def test_tampered_signature(self):
        token = make_oauth_state(SECRET, WS_ID, USER_ID)
        head, _ = token.split(".", 1)
        tampered = head + ".AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        assert verify_oauth_state(SECRET, tampered) is None

    def test_tampered_payload(self):
        token = make_oauth_state(SECRET, WS_ID, USER_ID)
        head, tail = token.split(".", 1)
        # Flip one char in the payload section; signature no longer matches.
        head = ("A" if head[0] != "A" else "B") + head[1:]
        assert verify_oauth_state(SECRET, head + "." + tail) is None

    def test_expired_token(self):
        token = make_oauth_state(SECRET, WS_ID, USER_ID)
        real_time = time.time
        try:
            time.time = lambda: real_time() + 3600   # jump 1 h ahead
            assert verify_oauth_state(SECRET, token) is None
        finally:
            time.time = real_time

    def test_missing_workspace_id_rejected(self):
        # Craft a token that is otherwise valid but omits workspace_id.
        import base64, hashlib, hmac, json, secrets as _secrets

        payload = {
            "user_id": USER_ID,
            "exp":     int(time.time()) + 300,
            "nonce":   _secrets.token_urlsafe(8),
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()
        p64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        s64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        token = f"{p64}.{s64}"
        assert verify_oauth_state(SECRET, token) is None

    def test_missing_user_id_rejected(self):
        import base64, hashlib, hmac, json, secrets as _secrets

        payload = {
            "workspace_id": WS_ID,
            "exp":          int(time.time()) + 300,
            "nonce":        _secrets.token_urlsafe(8),
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()
        p64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        s64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        token = f"{p64}.{s64}"
        assert verify_oauth_state(SECRET, token) is None

    def test_non_json_payload_rejected(self):
        # Payload that is valid base64 but not JSON.
        import base64, hashlib, hmac

        raw = b"this-is-not-json"
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()
        p64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        s64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        assert verify_oauth_state(SECRET, f"{p64}.{s64}") is None

    def test_json_array_rejected(self):
        # JSON but not a dict.
        import base64, hashlib, hmac, json

        raw = json.dumps([1, 2, 3]).encode()
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()
        p64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        s64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        assert verify_oauth_state(SECRET, f"{p64}.{s64}") is None


# ── Cross-connector isolation ────────────────────────────────────────────

class TestCrossConnectorIsolation:
    """A token signed with the Slack secret must not verify with the Gmail secret."""

    def test_slack_token_rejected_by_gmail_secret(self):
        slack_secret = "slack-secret"
        gmail_secret = "gmail-secret"

        token = make_oauth_state(slack_secret, WS_ID, USER_ID)
        # Verifying with a different secret must fail.
        assert verify_oauth_state(gmail_secret, token) is None
