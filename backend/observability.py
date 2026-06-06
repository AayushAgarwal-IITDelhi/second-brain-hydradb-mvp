"""
Phase 7: observability hooks for the Second Brain backend.

This module centralizes three concerns that previously lived scattered
across main.py and ad-hoc print statements:

  1. Sentry integration (opt-in via SENTRY_DSN). Initialized once in
     the FastAPI lifespan. Captures unhandled exceptions, attaches the
     request_id/user_id/workspace_id we're already logging, and tags
     events with environment+version so dashboards can slice by deploy.

  2. Dead-letter logging. Background jobs (Slack ingest, realtime
     event processing) that ultimately fail after retries call
     `emit_dead_letter(...)` -- the failure is logged with a stable
     "dead_letter" event tag plus enough context (workspace_id,
     payload digest, error class) to find and replay the failure
     offline. Sentry, if configured, ALSO receives the failure as a
     captured exception so on-call notifications fire.

  3. Dependency health checks. /api/ready calls these to confirm the
     backend can actually serve traffic: Supabase reachable, HydraDB
     reachable, OpenAI provider reachable. Each check has a tight
     timeout so a slow upstream doesn't make readiness hang.

Sentry is a SOFT dependency. If sentry_sdk isn't installed (e.g. in
tests, or in a minimal local dev setup), every Sentry helper here
becomes a no-op. The same code paths run in prod and in tests, so
behavior under test is deterministic without mocking.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import requests

from logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------- #
# Sentry init — opt-in via SENTRY_DSN
# ---------------------------------------------------------------------- #
# We do the import lazily inside init_sentry() so the module imports
# cleanly even if sentry_sdk isn't installed. Tests don't need it.

_sentry_enabled: bool = False


def sentry_enabled() -> bool:
    """True iff Sentry was successfully initialized this process."""
    return _sentry_enabled


def init_sentry() -> bool:
    """
    Initialize Sentry if SENTRY_DSN is set. Returns True on successful
    init, False if Sentry is disabled or sentry_sdk isn't installed.

    Idempotent: calling twice in the same process is a no-op after the
    first success.
    """
    global _sentry_enabled

    if _sentry_enabled:
        return True

    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        logger.info("sentry_disabled", extra={"reason": "no_dsn"})
        return False

    try:
        import sentry_sdk  # noqa: PLC0415
        from sentry_sdk.integrations.fastapi import FastApiIntegration  # noqa: PLC0415
        from sentry_sdk.integrations.starlette import StarletteIntegration  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        # sentry-sdk not installed (or its FastAPI integration missing).
        # Soft-fail: log and proceed. Production deploys that want Sentry
        # must include sentry-sdk in requirements.txt.
        logger.warning(
            "sentry_import_failed",
            extra={"error": type(e).__name__},
        )
        return False

    environment = (os.getenv("ENVIRONMENT") or "local").strip()
    release = (
        os.getenv("APP_VERSION")
        or os.getenv("RENDER_GIT_COMMIT")
        or os.getenv("RAILWAY_GIT_COMMIT_SHA")
        or "dev"
    ).strip()

    # Sample rates: 100% errors (default), 10% traces. Tunable via env.
    try:
        traces_sample_rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))
    except ValueError:
        traces_sample_rate = 0.1

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release,
            traces_sample_rate=traces_sample_rate,
            send_default_pii=False,   # never auto-attach IPs / cookies
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "sentry_init_failed",
            extra={"error": type(e).__name__},
        )
        return False

    _sentry_enabled = True
    logger.info(
        "sentry_initialized",
        extra={"environment": environment, "release": release},
    )
    return True


def capture_exception(
    err: BaseException,
    *,
    tags: Optional[Dict[str, str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Forward an exception to Sentry. No-op if Sentry isn't initialized.
    Always safe to call.
    """
    if not _sentry_enabled:
        return
    try:
        import sentry_sdk  # noqa: PLC0415
        with sentry_sdk.push_scope() as scope:
            for k, v in (tags or {}).items():
                if v:
                    scope.set_tag(k, v)
            for k, v in (extra or {}).items():
                scope.set_extra(k, v)
            sentry_sdk.capture_exception(err)
    except Exception as e:  # noqa: BLE001
        # Don't let an observability failure mask the original error.
        logger.warning(
            "sentry_capture_failed",
            extra={"error": type(e).__name__},
        )


# ---------------------------------------------------------------------- #
# Dead-letter logging for permanently-failed background work
# ---------------------------------------------------------------------- #
def emit_dead_letter(
    *,
    kind: str,
    workspace_id: str,
    error: BaseException,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Log a permanent background-job failure with a stable "dead_letter"
    event tag, and forward to Sentry if configured.

    `kind` is a short slug identifying the job class -- e.g.
    "slack_ingest", "realtime_event", "scheduler_pass". `context`
    carries small, log-safe details (counts, IDs, channel IDs) so the
    failure can be replayed without trawling Slack.

    DO NOT pass raw tokens or message text in `context`. Treat this
    like a public log line.
    """
    payload = dict(context or {})
    payload.update({
        "kind":         kind,
        "workspace_id": workspace_id,
        "error":        type(error).__name__,
        "error_msg":    str(error)[:300],   # truncate to keep logs bounded
    })
    logger.error("dead_letter", extra=payload)
    capture_exception(
        error,
        tags={"dead_letter": kind, "workspace_id": workspace_id},
        extra=payload,
    )


# ---------------------------------------------------------------------- #
# Readiness checks for /api/ready
# ---------------------------------------------------------------------- #
# Each check is a shallow ping: it should fail fast on a real outage
# and succeed in <500ms on a healthy upstream. We never run the LLM
# itself in readiness -- that would burn tokens on every probe.

# Tight timeouts so /api/ready never hangs the load balancer.
_DEP_TIMEOUT_SECONDS = 3.0


def _check_supabase() -> Dict[str, Any]:
    """
    Verify the Supabase auth service is reachable. We hit the
    /auth/v1/health endpoint which doesn't require any auth.
    """
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    if not url:
        return {"name": "supabase", "ok": False, "reason": "no_url"}
    started = time.monotonic()
    try:
        resp = requests.get(
            f"{url}/auth/v1/health",
            timeout=_DEP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        return {
            "name":      "supabase",
            "ok":        False,
            "reason":    type(e).__name__,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "name":       "supabase",
        "ok":         200 <= resp.status_code < 500,
        "status":     resp.status_code,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def _check_hydradb() -> Dict[str, Any]:
    """
    Verify HydraDB's API is reachable. We do an OPTIONS to the base
    URL so we don't consume any quota. Any 2xx-4xx response counts as
    "reachable"; only timeouts / 5xx mark it as unhealthy.
    """
    base = (os.getenv("HYDRADB_BASE_URL") or "https://api.hydradb.com").strip().rstrip("/")
    started = time.monotonic()
    try:
        resp = requests.options(base, timeout=_DEP_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        return {
            "name":       "hydradb",
            "ok":         False,
            "reason":     type(e).__name__,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "name":       "hydradb",
        "ok":         200 <= resp.status_code < 500,
        "status":     resp.status_code,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def _check_openai() -> Dict[str, Any]:
    """
    Verify the OpenAI (or compatible) provider is reachable. We hit
    the base URL with a HEAD request -- enough to confirm DNS + TLS +
    HTTP plumbing without burning model tokens.

    Skipped (returns ok=True) when DISABLE_OPENAI_READINESS=true,
    which is useful for self-hosted models that don't expose a
    standard health route.
    """
    if (os.getenv("DISABLE_OPENAI_READINESS") or "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return {"name": "openai", "ok": True, "skipped": True}
    base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com").strip().rstrip("/")
    started = time.monotonic()
    try:
        resp = requests.head(base, timeout=_DEP_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        return {
            "name":       "openai",
            "ok":         False,
            "reason":     type(e).__name__,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    # OpenAI's API returns 405/421 on HEAD to the base URL; that's
    # still "reachable" for our purposes. Only timeouts and 5xx fail.
    return {
        "name":       "openai",
        "ok":         resp.status_code < 500,
        "status":     resp.status_code,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def check_dependencies() -> Dict[str, Any]:
    """
    Run every readiness check and return a single summary dict.

    Shape:
        {
            "ok": bool,                          # AND of all checks
            "checks": [
                {"name": "supabase", "ok": ..., "latency_ms": ..., ...},
                {"name": "hydradb",  "ok": ..., ...},
                {"name": "openai",   "ok": ..., ...},
            ],
        }
    """
    checks = [
        _check_supabase(),
        _check_hydradb(),
        _check_openai(),
    ]
    overall_ok = all(c.get("ok") for c in checks)
    return {"ok": overall_ok, "checks": checks}