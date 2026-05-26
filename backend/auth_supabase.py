"""
Supabase JWT verification and workspace resolution for FastAPI.

Multi-user auth backbone. Replaces X-API-Key on user-facing routes
(/api/query, /api/query/stream, /api/me*, /api/saved-answers,
/api/chat/*, /api/slack/*). Admin/internal routes still use X-API-Key
(see auth.py).

Auth flow:
    Frontend -> Authorization: Bearer <supabase_jwt>
                X-Workspace-Id: <uuid>

    Backend  -> verify JWT (HS256 with shared secret, OR
                            ES256/RS256 via JWKS — see below)
              -> look up workspace_members via service-role Supabase client
              -> 401 / 400 / 403 on failure

Signing-algorithm support
-------------------------
Supabase used to sign Auth tokens exclusively with HS256 using the
project JWT secret (SUPABASE_JWT_SECRET). Recent projects ship with
the "JWT Signing Keys" feature, where tokens are signed asymmetrically
(ES256 by default, RS256 also possible) and the public keys are
served at

    {SUPABASE_URL}/auth/v1/.well-known/jwks.json

We support BOTH transparently:

  - The token's protected header is parsed first (no signature check).
  - If `alg` is HS256, we verify against SUPABASE_JWT_SECRET.
  - If `alg` is ES256 or RS256, we fetch (and cache) the matching key
    from JWKS by `kid` and verify with that public key.
  - Anything else -> 401.

PyJWT's PyJWKClient handles JWKS fetching + caching + key rotation —
we just wrap it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import jwt  # PyJWT
from fastapi import Header, HTTPException, status

from logging_config import get_logger
from supabase_client import get_workspace_membership

logger = get_logger(__name__)


# Defaults preserved for backwards compatibility with anything that
# imports them (notably the test suite, which forges HS256 tokens
# signed with the project secret).
SUPABASE_JWT_AUDIENCE = "authenticated"
SUPABASE_JWT_ALGORITHM = "HS256"

# Asymmetric algorithms Supabase emits when JWT Signing Keys are
# enabled. ES256 is the default; RS256 is also supported by the
# Supabase Auth server.
_ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")

# Total set of algorithms we'll accept on any incoming token. Anything
# else (e.g. "none", HS384, HS512) is rejected before any crypto runs.
_ACCEPTED_ALGORITHMS = ("HS256",) + _ASYMMETRIC_ALGORITHMS


@dataclass(frozen=True)
class SupabaseUser:
    """The verified caller. Built from the JWT's claims — no DB hit."""
    id: str
    email: Optional[str]


@dataclass(frozen=True)
class WorkspaceContext:
    """A verified user + the workspace they have access to."""
    user: SupabaseUser
    workspace_id: str
    role: str  # 'owner' | 'admin' | 'member'


# ---------- internal helpers ---------- #

def _jwt_secret() -> str:
    """
    Resolve the HS256 JWT secret at request time so a value change
    doesn't require a process restart. Fail-closed when blank: better
    to reject every request loudly than silently disable auth.

    Only called for HS256 tokens. Asymmetric tokens (ES256 / RS256)
    don't need it — they verify against the JWKS public keys.
    """
    value = (os.getenv("SUPABASE_JWT_SECRET") or "").strip()
    if not value:
        # 500 because this is a server-side config problem, not a client
        # problem. The startup validator should have caught this; if we
        # got here, someone unset the env after boot.
        logger.error('supabase_jwt_secret_missing')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server auth not configured.",
        )
    return value


def _supabase_url() -> str:
    """SUPABASE_URL with trailing slashes trimmed."""
    return (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")


def _jwks_url() -> str:
    """The Supabase project's JWKS endpoint."""
    base = _supabase_url()
    if not base:
        return ""
    return f"{base}/auth/v1/.well-known/jwks.json"


@lru_cache(maxsize=4)
def _jwks_client_for(url: str) -> "jwt.PyJWKClient":
    """
    Build a JWKS client per JWKS URL, cached for the process lifetime.
    PyJWKClient itself caches the fetched key set with a 5-minute
    lifespan (its `lifespan=300` default), so key rotations propagate
    promptly without us issuing a fresh HTTP fetch per request.

    Keyed by URL so a SUPABASE_URL change at runtime (rare; mostly
    tests via monkeypatch) doesn't keep using a stale client.
    """
    return jwt.PyJWKClient(
        url,
        cache_keys=True,
        max_cached_keys=16,
        cache_jwk_set=True,
        lifespan=300,
        timeout=10,
    )


def reset_jwks_cache() -> None:
    """Test hook: drop the cached PyJWKClient(s)."""
    _jwks_client_for.cache_clear()


def _peek_unverified_header(token: str) -> dict:
    """
    Read the JWT's protected header WITHOUT verifying anything yet.
    PyJWT does not validate the signature here. We use this only to
    look at `alg` and `kid` so we can route to the right verify path.
    """
    try:
        return jwt.get_unverified_header(token) or {}
    except jwt.InvalidTokenError:
        # Malformed token — let _decode_bearer's generic catch handle
        # it with a consistent 401. Return an empty header so the
        # caller's algorithm switch falls through to the 401 path.
        return {}


def _verify_jwt(token: str) -> dict:
    """
    Verify `token` and return its claims.

    Strategy:
      1. Peek at the unverified header to discover `alg` and `kid`.
      2. If alg is HS256: verify with SUPABASE_JWT_SECRET.
      3. If alg is asymmetric (ES256 / RS256): pull the matching key
         from JWKS by `kid` and verify with that public key.
      4. Anything else: raise jwt.InvalidTokenError so the outer
         handler turns it into a 401.

    Raises:
      jwt.ExpiredSignatureError  — caller maps to 401 "Token expired."
      jwt.InvalidAudienceError   — caller maps to 401 "Invalid token audience."
      jwt.InvalidTokenError      — caller maps to 401 "Invalid token."
    """
    header = _peek_unverified_header(token)
    alg = (header.get("alg") or "").strip()

    # If the header was unparseable, default to HS256. This is both:
    #   - what Supabase emits when JWT Signing Keys aren't enabled
    #     (the project secret signs everything), and
    #   - what the test suite forges, so a blank SUPABASE_JWT_SECRET
    #     still fails closed with the 500 from _jwt_secret() instead
    #     of leaking a 401 that could let an operator miss the
    #     misconfiguration.
    if not alg:
        alg = "HS256"

    if alg not in _ACCEPTED_ALGORITHMS:
        # Catch "none", HS384, HS512, weird typos. Treat as a generic
        # invalid token so we don't leak which check rejected it.
        raise jwt.InvalidTokenError(
            f"Unsupported JWT algorithm: {alg or 'missing'}"
        )

    common_kwargs = {
        "algorithms":  [alg],
        "audience":    SUPABASE_JWT_AUDIENCE,
        "options":     {"require": ["sub", "exp"]},
    }

    if alg == "HS256":
        # Symmetric path. Uses the project's shared JWT secret.
        # _jwt_secret() raises HTTPException(500) if the env var is
        # blank — that's the intended fail-closed behavior.
        return jwt.decode(token, _jwt_secret(), **common_kwargs)

    # Asymmetric path (ES256 / RS256). The public keys live in JWKS.
    url = _jwks_url()
    if not url:
        logger.error('supabase_url_missing_for_jwks')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server auth not configured.",
        )

    try:
        signing_key = _jwks_client_for(url).get_signing_key_from_jwt(token)
    except jwt.PyJWKClientError as e:
        # Unknown kid, network blip, malformed JWKS, etc. Don't leak
        # the specific cause — collapse to InvalidTokenError so the
        # outer handler returns a generic 401.
        logger.warning(
            'supabase_jwks_lookup_failed',
            extra={'error': type(e).__name__, 'kid': header.get('kid')},
        )
        raise jwt.InvalidTokenError("Could not resolve signing key.") from e

    return jwt.decode(token, signing_key.key, **common_kwargs)


def _decode_bearer(authorization: Optional[str]) -> SupabaseUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token.",
        )

    try:
        claims = _verify_jwt(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired.",
        )
    except jwt.InvalidAudienceError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token audience.",
        )
    except jwt.InvalidTokenError:
        # Don't leak which check failed — bad signature, bad iss,
        # unsupported alg, missing kid, etc. all map to the same
        # client-visible response.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
        )

    user_id = (claims.get("sub") or "").strip()
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject.",
        )

    email = claims.get("email")
    if email is not None and not isinstance(email, str):
        email = None
    return SupabaseUser(id=user_id, email=email)


# ---------- FastAPI dependencies ---------- #

def require_user(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> SupabaseUser:
    """
    Verifies the Supabase JWT and returns a SupabaseUser.

    Raises 401 on any verification failure.
    """
    user = _decode_bearer(authorization)
    # Phase 7: bind the verified identity into the per-request logging
    # context so every downstream log line carries user_id automatically.
    # workspace_id stays None on user-only routes (/api/me*).
    from logging_config import bind_user_context  # noqa: PLC0415
    bind_user_context(user.id, None)
    return user


def require_workspace(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_workspace_id: Optional[str] = Header(default=None, alias="X-Workspace-Id"),
) -> WorkspaceContext:
    """
    Verifies the JWT AND that the user is a member of the workspace named
    in X-Workspace-Id.

    Status codes:
        401 — token missing or invalid
        400 — workspace header missing or blank
        403 — user is not a member of the workspace
    """
    user = _decode_bearer(authorization)

    workspace_id = (x_workspace_id or "").strip()
    if not workspace_id:
        # Bind the user even though the workspace header is missing,
        # so the resulting 400 log line still carries user_id.
        from logging_config import bind_user_context  # noqa: PLC0415
        bind_user_context(user.id, None)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Workspace-Id header.",
        )

    role = get_workspace_membership(user_id=user.id, workspace_id=workspace_id)
    if role is None:
        # 403 not 404, so workspace-id enumeration can't probe existence.
        from logging_config import bind_user_context  # noqa: PLC0415
        bind_user_context(user.id, workspace_id)
        logger.info(
            'workspace_access_denied',
            extra={'user_id': user.id, 'workspace_id': workspace_id},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to this workspace.",
        )

    # Phase 7: bind both user_id AND workspace_id for every downstream
    # log line on this request. This is what gives the workspace-aware
    # observability surface its punch.
    from logging_config import bind_user_context  # noqa: PLC0415
    bind_user_context(user.id, workspace_id)

    return WorkspaceContext(
        user=user, workspace_id=workspace_id, role=role,
    )