"""
Per-process in-memory rate limiter for the Second Brain backend.

Phase 7 hardening
-----------------
The original implementation had a single global bucket and one limit.
Phase 7 adds:

  - Per-route buckets via make_rate_limit_dependency(bucket_name, limit).
    Each bucket maintains its own client->timestamps map, so a flood
    of /slack/events from one workspace can't starve /api/query
    requests from another user.
  - Per-bucket default limits (env-overridable):
        auth           -> RATE_LIMIT_AUTH_PER_5_MIN          (default 30)
        query          -> RATE_LIMIT_PER_5_MIN               (default 20, legacy)
        slack_webhook  -> RATE_LIMIT_SLACK_WEBHOOK_PER_5_MIN (default 600)
        ingest         -> RATE_LIMIT_INGEST_PER_5_MIN        (default 5)
  - The legacy rate_limit_dependency remains exported and continues to
    target the "query" bucket so existing routes keep working.

Trade-offs (unchanged from MVP):
  - In-memory, per-process. State resets on restart and isn't shared
    across workers. Fine for a single-instance deployment; swap for
    Redis if you horizontally scale the backend.
"""

import os
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Callable, Deque, Dict, Optional

from fastapi import Request

from errors import RateLimitedError
from logging_config import get_logger

logger = get_logger(__name__)

WINDOW_SECONDS = 5 * 60  # 5 minutes


# ---------------------------------------------------------------------- #
# Per-bucket limit lookup
# ---------------------------------------------------------------------- #
# Each bucket reads its limit from env at REQUEST time so it can be
# tuned without a restart. The default per bucket is hand-picked for
# its typical request profile:
#
#   auth          — low. Defends against credential-stuffing on
#                   /api/me and /api/me/workspaces. 30/5min is plenty
#                   for legitimate page loads.
#   query         — moderate. The user-facing query route. The legacy
#                   default (20/5min) is preserved.
#   slack_webhook — high. Slack can burst events during a busy
#                   ingestion window. 600/5min = 2 events/sec sustained.
#   ingest        — low. Manual /api/slack/ingest is expensive
#                   (HydraDB uploads). 5/5min stops accidental loops.
_BUCKET_DEFAULTS = {
    "auth":          ("RATE_LIMIT_AUTH_PER_5_MIN",           30),
    "query":         ("RATE_LIMIT_PER_5_MIN",                20),
    "slack_webhook": ("RATE_LIMIT_SLACK_WEBHOOK_PER_5_MIN",  600),
    "ingest":        ("RATE_LIMIT_INGEST_PER_5_MIN",         5),
}


def _bucket_limit(bucket: str, override: Optional[int] = None) -> int:
    """
    Resolve the configured limit for a bucket. `override` short-circuits
    env lookup -- used by tests that want a deterministic value without
    mutating os.environ.
    """
    if override is not None:
        return max(1, int(override))
    env_name, default = _BUCKET_DEFAULTS.get(bucket, ("RATE_LIMIT_PER_5_MIN", 100))
    try:
        return max(1, int(os.getenv(env_name, str(default))))
    except ValueError:
        return default


# ---------------------------------------------------------------------- #
# Client identification
# ---------------------------------------------------------------------- #
def _client_id_from(request: Request) -> str:
    """
    Identify the client for rate-limit accounting.

    Order of preference:
      1. Supabase user id (sub from the bound logging context, set by
         require_user / require_workspace) -- so a single user is
         throttled across workspaces.
      2. X-API-Key header (legacy admin clients).
      3. Remote IP.
    """
    # Avoid an import cycle by reading the contextvar from logging_config
    # only when needed. Falls back gracefully if it's unset.
    try:
        from logging_config import _user_id  # noqa: PLC0415
        uid = _user_id.get()
        if uid:
            return f"user:{uid}"
    except Exception:  # noqa: BLE001
        pass

    key = request.headers.get("x-api-key")
    if key:
        return f"key:{key}"
    ip = request.client.host if request.client else "unknown"
    return f"ip:{ip}"


# ---------------------------------------------------------------------- #
# Sliding-window storage
# ---------------------------------------------------------------------- #
class _BucketedLimiter:
    """
    One sliding-window deque PER (bucket, client_id) tuple. A flood in
    one bucket doesn't affect any other bucket.
    """

    def __init__(self) -> None:
        # outer: bucket name -> inner dict
        # inner: client_id -> deque of monotonic timestamps
        self._buckets: Dict[str, Dict[str, Deque[float]]] = defaultdict(dict)
        self._lock = Lock()

    def hit(self, bucket: str, client_id: str, limit: int) -> None:
        now = time.monotonic()
        cutoff = now - WINDOW_SECONDS

        with self._lock:
            bucket_map = self._buckets[bucket]
            dq = bucket_map.get(client_id)
            if dq is None:
                dq = deque()
                bucket_map[client_id] = dq
            while dq and dq[0] < cutoff:
                dq.popleft()

            if len(dq) >= limit:
                retry_after = max(1, int(dq[0] + WINDOW_SECONDS - now))
                logger.info(
                    "rate_limit_exceeded",
                    extra={
                        "bucket":      bucket,
                        "client_id":   client_id,
                        "count":       len(dq),
                        "limit":       limit,
                        "retry_after": retry_after,
                    },
                )
                raise RateLimitedError(
                    detail=(
                        f"Too many requests on {bucket} "
                        f"(limit {limit} per 5 minutes). "
                        f"Try again in about {retry_after} seconds."
                    ),
                    log_context=(
                        f"bucket={bucket} client={client_id} "
                        f"count={len(dq)} limit={limit}"
                    ),
                )

            dq.append(now)


_limiter = _BucketedLimiter()


# ---------------------------------------------------------------------- #
# FastAPI dependencies
# ---------------------------------------------------------------------- #
def make_rate_limit_dependency(
    bucket: str,
    *,
    limit: Optional[int] = None,
) -> Callable[[Request], None]:
    """
    Build a FastAPI dependency that rate-limits requests against
    `bucket`. Each bucket is independent.

    Usage:
        slack_webhook_limit = make_rate_limit_dependency("slack_webhook")
        @app.post("/slack/events", dependencies=[Depends(slack_webhook_limit)])

    `limit` overrides the env-driven default; useful for tests.
    """
    if bucket not in _BUCKET_DEFAULTS:
        # Allow ad-hoc bucket names but log a warning so a typo doesn't
        # silently degrade to RATE_LIMIT_PER_5_MIN.
        logger.warning("rate_limit_unknown_bucket", extra={"bucket": bucket})

    def _dep(request: Request) -> None:
        cid = _client_id_from(request)
        eff_limit = _bucket_limit(bucket, override=limit)
        _limiter.hit(bucket, cid, eff_limit)

    return _dep


# Backwards-compatibility shim: the legacy global function targets the
# "query" bucket. Existing routes that use this dependency keep working.
def rate_limit_dependency(request: Request) -> None:
    """Legacy single-bucket dependency. Targets the 'query' bucket."""
    cid = _client_id_from(request)
    _limiter.hit("query", cid, _bucket_limit("query"))
