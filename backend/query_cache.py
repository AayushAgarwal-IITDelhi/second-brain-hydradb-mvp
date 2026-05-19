"""
In-memory TTL cache for /api/query responses.

Keyed by the full set of request parameters (question + top_k + mode +
optional filters) so two identical requests within the TTL window get
the same response without hitting HydraDB or the LLM.

State is per-process and resets on restart — that's intentional for the
MVP. Swap for Redis if you scale horizontally.
"""

import hashlib
import json
import os
import threading
from typing import Any, Dict, Optional, Tuple

from cachetools import TTLCache


def _enabled() -> bool:
    return os.getenv("QUERY_CACHE_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _ttl_seconds() -> int:
    try:
        return max(1, int(os.getenv("QUERY_CACHE_TTL_SECONDS", "300")))
    except ValueError:
        return 300


def _max_size() -> int:
    try:
        return max(1, int(os.getenv("QUERY_CACHE_MAX_SIZE", "100")))
    except ValueError:
        return 100


# Lazily build a single cache instance using the env-configured size + TTL.
# The lock guards both construction and reads/writes — TTLCache itself isn't
# thread-safe on concurrent get-or-set patterns.
_cache: Optional[TTLCache] = None
_cache_config: Optional[Tuple[int, int]] = None  # (max_size, ttl) used at build
_lock = threading.Lock()


def _get_cache() -> TTLCache:
    """Build the cache on first use; rebuild if size/ttl env changed."""
    global _cache, _cache_config
    desired = (_max_size(), _ttl_seconds())
    if _cache is None or _cache_config != desired:
        _cache = TTLCache(maxsize=desired[0], ttl=desired[1])
        _cache_config = desired
    return _cache


def build_cache_key(params: Dict[str, Any]) -> str:
    """
    Stable key from the request parameter dict.

    `params` should already be normalized (whitespace-stripped strings,
    None for missing optional fields). We sort keys so dict ordering
    never matters.
    """
    canonical = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_cached(key: str) -> Optional[Dict[str, Any]]:
    """Return a deep-enough copy if cached, else None."""
    if not _enabled():
        return None
    with _lock:
        cached = _get_cache().get(key)
    if cached is None:
        return None
    # Shallow copy + a fresh debug dict so we can add cache_hit without
    # mutating the cached object for the next caller.
    result = dict(cached)
    result["debug"] = {**(cached.get("debug") or {}), "cache_hit": True}
    return result


def put(key: str, value: Dict[str, Any]) -> None:
    if not _enabled():
        return
    with _lock:
        _get_cache()[key] = value


def invalidate_all() -> None:
    """Wipe the cache. Useful in tests and after fresh ingestion runs."""
    with _lock:
        if _cache is not None:
            _cache.clear()