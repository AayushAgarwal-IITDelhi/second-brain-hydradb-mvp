"""
Slack request signature verification.

Slack signs every webhook payload using HMAC-SHA256 over the string
    "v0:<timestamp>:<raw body>"
with SLACK_SIGNING_SECRET as the key. The signature header looks like:
    X-Slack-Signature: v0=<hex digest>
The timestamp header is:
    X-Slack-Request-Timestamp: <unix seconds>

We:
- reject timestamps more than ~5 minutes from now (replay protection),
- compute the expected signature and compare with hmac.compare_digest,
- return a boolean (no exceptions raised), so the caller can log + 401
  cleanly.

If SLACK_SIGNING_SECRET is unset we refuse to verify any request (fail
closed). The /slack/events route fails open ONLY if you explicitly turn
off realtime ingestion AND remove the env var — in that case the route
returns 503 and Slack will disable the integration.
"""

import hashlib
import hmac
import os
import time
from typing import Optional


# Slack recommends rejecting timestamps more than 5 minutes off.
SIGNATURE_MAX_AGE_SECONDS = 60 * 5


def _signing_secret() -> Optional[str]:
    raw = os.getenv("SLACK_SIGNING_SECRET", "").strip()
    return raw or None


def verify_slack_signature(
    body: bytes,
    timestamp_header: Optional[str],
    signature_header: Optional[str],
) -> bool:
    """
    Return True if the request body + headers match a valid Slack signature.

    Args:
        body:             raw request body as bytes (NOT json-reparsed).
        timestamp_header: value of X-Slack-Request-Timestamp.
        signature_header: value of X-Slack-Signature, e.g. "v0=abcdef...".
    """
    secret = _signing_secret()
    if not secret:
        # Fail closed: without a signing secret we can't verify anything.
        return False

    if not timestamp_header or not signature_header:
        return False

    # Replay protection: must be a parseable integer-ish unix seconds and
    # within SIGNATURE_MAX_AGE_SECONDS of now.
    try:
        ts_int = int(float(timestamp_header))
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts_int) > SIGNATURE_MAX_AGE_SECONDS:
        return False

    # `body` may legitimately be empty bytes; we still HMAC over the
    # concatenation. Build the base string per Slack's spec.
    base_string = b"v0:" + str(ts_int).encode("utf-8") + b":" + body

    expected_digest = hmac.new(
        secret.encode("utf-8"),
        base_string,
        hashlib.sha256,
    ).hexdigest()
    expected_header = f"v0={expected_digest}"

    return hmac.compare_digest(expected_header, signature_header)