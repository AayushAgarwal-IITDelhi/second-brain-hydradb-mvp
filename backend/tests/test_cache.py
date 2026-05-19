"""Tests for query_cache.py — in-memory TTL cache."""

import os
import time
from unittest.mock import patch

import pytest


class TestBuildCacheKey:
    def test_deterministic(self):
        from query_cache import build_cache_key
        params = {"question": "hello", "top_k": 5, "mode": "default"}
        assert build_cache_key(params) == build_cache_key(params)

    def test_different_params_different_keys(self):
        from query_cache import build_cache_key
        k1 = build_cache_key({"question": "hello", "top_k": 5})
        k2 = build_cache_key({"question": "world", "top_k": 5})
        assert k1 != k2

    def test_dict_order_independent(self):
        from query_cache import build_cache_key
        k1 = build_cache_key({"a": 1, "b": 2})
        k2 = build_cache_key({"b": 2, "a": 1})
        assert k1 == k2

    def test_returns_hex_string(self):
        from query_cache import build_cache_key
        key = build_cache_key({"q": "test"})
        assert isinstance(key, str)
        assert len(key) == 64  # sha256 hex digest

    def test_none_values_handled(self):
        from query_cache import build_cache_key
        k1 = build_cache_key({"channel": None, "user": None})
        k2 = build_cache_key({"channel": None, "user": "alice"})
        assert k1 != k2


class TestGetCached:
    def setup_method(self):
        from query_cache import invalidate_all
        invalidate_all()

    def test_miss_returns_none(self):
        from query_cache import get_cached, build_cache_key
        with patch.dict(os.environ, {"QUERY_CACHE_ENABLED": "true"}):
            assert get_cached("nonexistent-key") is None

    def test_cache_disabled_always_misses(self):
        from query_cache import get_cached, put, build_cache_key
        with patch.dict(os.environ, {"QUERY_CACHE_ENABLED": "false"}):
            put("mykey", {"answer": "hello"})
            assert get_cached("mykey") is None

    def test_hit_after_put(self):
        from query_cache import get_cached, put
        with patch.dict(os.environ, {"QUERY_CACHE_ENABLED": "true"}):
            put("mykey", {"answer": "cached response", "sources": []})
            result = get_cached("mykey")
            assert result is not None
            assert result["answer"] == "cached response"

    def test_hit_sets_cache_hit_true(self):
        from query_cache import get_cached, put
        with patch.dict(os.environ, {"QUERY_CACHE_ENABLED": "true"}):
            put("key99", {"answer": "y", "debug": {"x": 1}})
            result = get_cached("key99")
            assert result["debug"]["cache_hit"] is True

    def test_original_not_mutated(self):
        """Returned value should not be the same dict object as stored."""
        from query_cache import get_cached, put
        with patch.dict(os.environ, {"QUERY_CACHE_ENABLED": "true"}):
            original = {"answer": "hello", "debug": {}}
            put("k", original)
            result = get_cached("k")
            result["debug"]["cache_hit"] = False  # mutate result
            # next read should still show True
            result2 = get_cached("k")
            assert result2["debug"]["cache_hit"] is True

    def test_invalidate_all_clears_cache(self):
        from query_cache import get_cached, put, invalidate_all
        with patch.dict(os.environ, {"QUERY_CACHE_ENABLED": "true"}):
            put("k123", {"answer": "x"})
            assert get_cached("k123") is not None
            invalidate_all()
            assert get_cached("k123") is None


class TestCacheConfig:
    def test_cache_enabled_by_default(self):
        from query_cache import _enabled
        with patch.dict(os.environ, {"QUERY_CACHE_ENABLED": "true"}):
            assert _enabled() is True

    @pytest.mark.parametrize("val", ["false", "0", "no", "off"])
    def test_cache_disabled_flags(self, val):
        from query_cache import _enabled
        with patch.dict(os.environ, {"QUERY_CACHE_ENABLED": val}):
            assert _enabled() is False

    def test_ttl_default(self):
        from query_cache import _ttl_seconds
        with patch.dict(os.environ, {"QUERY_CACHE_TTL_SECONDS": "300"}):
            assert _ttl_seconds() == 300

    def test_ttl_bad_value_returns_300(self):
        from query_cache import _ttl_seconds
        with patch.dict(os.environ, {"QUERY_CACHE_TTL_SECONDS": "bad"}):
            assert _ttl_seconds() == 300

    def test_ttl_minimum_1(self):
        from query_cache import _ttl_seconds
        with patch.dict(os.environ, {"QUERY_CACHE_TTL_SECONDS": "0"}):
            assert _ttl_seconds() == 1

    def test_max_size_bad_value_returns_100(self):
        from query_cache import _max_size
        with patch.dict(os.environ, {"QUERY_CACHE_MAX_SIZE": "abc"}):
            assert _max_size() == 100
