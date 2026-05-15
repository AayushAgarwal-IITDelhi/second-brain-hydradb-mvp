"""
Tiny header-based API key auth for the Second Brain MVP.

Usage in FastAPI routes:

    from fastapi import Depends
    from auth import require_api_key

    @app.post("/api/query", dependencies=[Depends(require_api_key)])
    def query(...): ...

Request must include:
    X-API-Key: <value of APP_API_KEY>

If the header is missing or doesn't match APP_API_KEY (from .env),
the request fails with HTTP 401 and body {"detail": "Unauthorized"}.

No JWT, no OAuth, no database — just a shared secret in an env var.
"""

import os
import secrets
from typing import Optional

from fastapi import Header, HTTPException, status


API_KEY_HEADER_NAME = "X-API-Key"


def _expected_api_key() -> Optional[str]:
    """
    Read the expected key from the environment at call time so the value
    can change without a restart. An empty / unset key is treated as
    "auth not configured" — see require_api_key below.
    """
    value = os.getenv("APP_API_KEY", "")
    value = value.strip()
    return value or None


def require_api_key(
    # FastAPI maps the function parameter name to the request header.
    # Hyphens become underscores: "X-API-Key" -> x_api_key.
    x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER_NAME),
) -> None:
    """
    FastAPI dependency. Raises 401 unless the request's X-API-Key header
    matches APP_API_KEY exactly. Uses a constant-time comparison so we
    don't leak the secret length via timing.

    Fail-closed: if APP_API_KEY is unset or blank, the dependency
    rejects all requests. That way a forgotten config doesn't silently
    leave protected routes wide open.
    """
    expected = _expected_api_key()
    if not expected:
        # Fail-closed when the server isn't configured — better to break
        # loudly than to silently disable auth in production.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )