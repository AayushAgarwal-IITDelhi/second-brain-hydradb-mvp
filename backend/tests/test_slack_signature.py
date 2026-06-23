"""Tests for slack_signature.py — HMAC-SHA256 webhook verification."""

import hashlib
import hmac
import os
import time
from unittest.mock import patch

import pytest


def _make_signature(body: bytes, secret: str, timestamp: int) -> str:
    base = b"v0:" + str(timestamp).encode() + b":" + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


SIGNING_SECRET = "test-slack-signing-secret"
TEST_BODY = b'{"type":"event_callback","event":{"type":"message"}}'


class TestVerifySlackSignature:
    def _ts(self) -> int:
        return int(time.time())

    def test_valid_signature_passes(self):
        from slack_signature import verify_slack_signature

        ts = self._ts()
        sig = _make_signature(TEST_BODY, SIGNING_SECRET, ts)
        with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": SIGNING_SECRET}):
            assert verify_slack_signature(TEST_BODY, str(ts), sig) is True

    def test_wrong_signature_fails(self):
        from slack_signature import verify_slack_signature

        ts = self._ts()
        with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": SIGNING_SECRET}):
            assert verify_slack_signature(TEST_BODY, str(ts), "v0=badhash") is False

    def test_missing_timestamp_fails(self):
        from slack_signature import verify_slack_signature

        with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": SIGNING_SECRET}):
            assert verify_slack_signature(TEST_BODY, None, "v0=something") is False

    def test_missing_signature_fails(self):
        from slack_signature import verify_slack_signature

        ts = self._ts()
        with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": SIGNING_SECRET}):
            assert verify_slack_signature(TEST_BODY, str(ts), None) is False

    def test_missing_secret_fails_closed(self):
        from slack_signature import verify_slack_signature

        ts = self._ts()
        sig = _make_signature(TEST_BODY, SIGNING_SECRET, ts)
        with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": ""}, clear=False):
            # Clear only the secret
            env_backup = os.environ.get("SLACK_SIGNING_SECRET")
            os.environ["SLACK_SIGNING_SECRET"] = ""
            try:
                result = verify_slack_signature(TEST_BODY, str(ts), sig)
            finally:
                if env_backup is not None:
                    os.environ["SLACK_SIGNING_SECRET"] = env_backup
                else:
                    os.environ.pop("SLACK_SIGNING_SECRET", None)
        assert result is False

    def test_stale_timestamp_fails(self):
        from slack_signature import SIGNATURE_MAX_AGE_SECONDS, verify_slack_signature

        old_ts = int(time.time()) - SIGNATURE_MAX_AGE_SECONDS - 60
        sig = _make_signature(TEST_BODY, SIGNING_SECRET, old_ts)
        with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": SIGNING_SECRET}):
            assert verify_slack_signature(TEST_BODY, str(old_ts), sig) is False

    def test_future_timestamp_fails(self):
        from slack_signature import SIGNATURE_MAX_AGE_SECONDS, verify_slack_signature

        future_ts = int(time.time()) + SIGNATURE_MAX_AGE_SECONDS + 60
        sig = _make_signature(TEST_BODY, SIGNING_SECRET, future_ts)
        with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": SIGNING_SECRET}):
            assert verify_slack_signature(TEST_BODY, str(future_ts), sig) is False

    def test_invalid_timestamp_format_fails(self):
        from slack_signature import verify_slack_signature

        with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": SIGNING_SECRET}):
            assert verify_slack_signature(TEST_BODY, "not-a-number", "v0=abc") is False

    def test_empty_body_is_valid(self):
        """Slack may send empty bodies for some events; HMAC over empty bytes is fine."""
        from slack_signature import verify_slack_signature

        ts = self._ts()
        sig = _make_signature(b"", SIGNING_SECRET, ts)
        with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": SIGNING_SECRET}):
            assert verify_slack_signature(b"", str(ts), sig) is True

    def test_body_mismatch_fails(self):
        from slack_signature import verify_slack_signature

        ts = self._ts()
        sig = _make_signature(b"original body", SIGNING_SECRET, ts)
        with patch.dict(os.environ, {"SLACK_SIGNING_SECRET": SIGNING_SECRET}):
            assert verify_slack_signature(b"tampered body", str(ts), sig) is False

    def test_signature_max_age_is_5_minutes(self):
        from slack_signature import SIGNATURE_MAX_AGE_SECONDS

        assert SIGNATURE_MAX_AGE_SECONDS == 300
