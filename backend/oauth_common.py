"""
Shared HMAC-signed OAuth state token helpers.

Both the Slack and Gmail connectors need exactly the same signed-state
pattern (tamper-evident token, nonce, 5-minute expiry) but each uses its
own secret so a credential leak in one connector cannot forge state for the
other.  This module provides the generic crypto primitives; each connector
wraps them with its connector-specific secret lookup.

Public API
----------
make_oauth_state(secret, workspace_id, user_id, *, lifetime_seconds=300)
    Build a signed state token.  ``secret`` must be non-empty; call-sites
    are responsible for the "fail closed if secret missing" guard.

verify_oauth_state(secret, state)
    Verify a state token.  Returns the decoded payload dict on success, or
    ``None`` on any failure (bad format, bad signature, expired, empty
    secret).  Never raises.

Token format
------------
    base64url(JSON-payload) "." base64url(HMAC-SHA256-signature)

Payload keys: workspace_id, user_id, exp (Unix timestamp), nonce.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------- #
# Encoding helpers
# ---------------------------------------------------------------------- #


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


# ---------------------------------------------------------------------- #
# Core token operations
# ---------------------------------------------------------------------- #


def make_oauth_state(
    secret: str,
    workspace_id: str,
    user_id: str,
    *,
    lifetime_seconds: int = 300,
) -> str:
    """
    Build a tamper-evident state token binding this OAuth attempt to a
    specific workspace + user.

    Includes a short expiry (``lifetime_seconds``) so a stolen state token
    cannot be replayed indefinitely, and a random nonce so two consecutive
    calls from the same workspace always produce distinct tokens.

    ``secret`` must be a non-empty HMAC key; callers are expected to
    validate it before calling (fail-closed pattern).

    Format: base64url(payload) "." base64url(signature)
    """
    payload = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "exp": int(time.time()) + lifetime_seconds,
        "nonce": secrets.token_urlsafe(8),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return _b64url_encode(raw) + "." + _b64url_encode(sig)


def verify_oauth_state(
    secret: str,
    state: str,
) -> Optional[Dict[str, Any]]:
    """
    Validate a signed state token.

    Returns the decoded payload dict on success, or ``None`` on any
    failure: empty/malformed input, wrong HMAC, expired token, missing
    required fields, or empty secret.  Never raises — callers branch on
    ``None``.
    """
    if not secret or not state or "." not in state:
        return None
    try:
        payload_b64, sig_b64 = state.split(".", 1)
        raw = _b64url_decode(payload_b64)
        sig = _b64url_decode(sig_b64)
    except Exception:  # noqa: BLE001
        return None

    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None

    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    if not payload.get("workspace_id") or not payload.get("user_id"):
        return None
    return payload
