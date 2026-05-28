"""
Startup-time environment validation for the Second Brain backend.

Called from main.py's FastAPI lifespan. If anything required is missing
or blank, we raise StartupConfigError with a clear, multi-line message
so uvicorn prints it and exits -- better than letting requests fail
mysteriously at 3am.

Phase 7 hardening
-----------------
- Production-mode checks. When ENVIRONMENT=production, additional
  guards refuse to start if the configuration looks unsafe (CORS
  pointing at localhost, missing FRONTEND_BASE_URL, OAuth state
  secret left at the .env.example placeholder, etc.).
- Secrets audit. At startup we log a REDACTED summary of which
  secrets are present so operators can confirm "did my deploy pick
  up the env vars I set?" without exposing the values themselves.
"""

import os
from typing import List

from logging_config import get_logger

logger = get_logger(__name__)


# Vars whose values are required to be set AND non-empty before the app
# can serve any request. Optional/tunable vars (LLM_MAX_TOKENS,
# DEBUG_RECALL, CORS_ORIGINS, etc.) are NOT validated here -- they have
# safe defaults.
REQUIRED_ENV_VARS = (
    "APP_API_KEY",
    "HYDRADB_API_KEY",
    "HYDRADB_TENANT_ID",
    "OPENAI_API_KEY",  # also used for OpenRouter and other OpenAI-compatible providers
    # --- Supabase (Phase 1 multi-user) ---------------------------------
    "SUPABASE_URL",
    "SUPABASE_JWT_SECRET",
    "SUPABASE_SERVICE_ROLE_KEY",
    # --- Slack Connect (Phase 3 per-workspace Slack OAuth) -------------
    # SLACK_BOT_TOKEN / SLACK_CHANNEL_IDS are still honored by the
    # prototype CLI ingestion (ingestion/ingest_slack.py) and the
    # realtime webhook so existing tests pass. New per-workspace
    # routes use the OAuth flow below instead.
    "SLACK_CLIENT_ID",
    "SLACK_CLIENT_SECRET",
    "SLACK_REDIRECT_URI",
    "SLACK_OAUTH_STATE_SECRET",
    "SLACK_SIGNING_SECRET",
)


# Common placeholder values from .env.example that MUST not survive
# into production. The audit logs a warning per match; the production
# check upgrades it to a hard failure.
_PLACEHOLDER_FRAGMENTS = (
    "replace-with",
    "your-secret",
    "your-slack",
    "your-supabase",
    "your-openai",
    "your-hydradb",
    "your-tenant",
    "your-app",
    "generate-a-long",
    "long-random-string",
    "do-not-use-in-prod",
)


class StartupConfigError(RuntimeError):
    """Raised when required configuration is missing at startup."""


def _is_production() -> bool:
    return (os.getenv("ENVIRONMENT") or "").strip().lower() == "production"


def _looks_like_placeholder(value: str) -> bool:
    """True if the value contains an obvious .env.example placeholder."""
    if not value:
        return False
    lower = value.lower()
    return any(frag in lower for frag in _PLACEHOLDER_FRAGMENTS)


def _validate_required_env() -> List[str]:
    """Return a list of missing/blank required env vars."""
    return [
        name for name in REQUIRED_ENV_VARS
        if not (os.getenv(name) or "").strip()
    ]


# Gmail Connect (Phase 8) is OPT-IN per deployment. Either you set the
# full group (all 4 vars), or you set none of them. A partial
# configuration is the dangerous case: the app boots clean, /api/ready
# stays green, the user clicks "Connect Gmail" and only THEN crashes
# with `RuntimeError("GMAIL_OAUTH_STATE_SECRET is not set.")`. Group
# validation closes that gap at boot.
_GMAIL_OAUTH_ENV_GROUP = (
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REDIRECT_URI",
    "GMAIL_OAUTH_STATE_SECRET",
)


def _validate_gmail_group() -> List[str]:
    """
    Gmail is opt-in. Returns:
      - []                      when ALL Gmail OAuth env vars are set (group complete),
      - []                      when NONE are set (group absent — opt out),
      - [missing names]         when SOME are set but not all (partial config).

    Partial configuration is a misconfiguration we want to catch at
    boot — see the comment on _GMAIL_OAUTH_ENV_GROUP.
    """
    present = [n for n in _GMAIL_OAUTH_ENV_GROUP if (os.getenv(n) or "").strip()]
    if not present:
        return []                       # all 4 unset -> Gmail off, fine.
    if len(present) == len(_GMAIL_OAUTH_ENV_GROUP):
        return []                       # all 4 set -> Gmail on, fine.
    # Partial: report the ones still missing.
    return [n for n in _GMAIL_OAUTH_ENV_GROUP if n not in present]


def _validate_production_env() -> List[str]:
    """
    Production-only sanity checks. Returns a list of human-readable
    issues; empty list means the production config looks safe.
    """
    issues: List[str] = []

    # CORS must not contain a localhost origin in production -- a
    # leftover dev origin would let any extension on localhost talk
    # to the prod backend.
    cors = (os.getenv("CORS_ORIGINS") or "").strip().lower()
    if cors and ("localhost" in cors or "127.0.0.1" in cors):
        issues.append(
            "CORS_ORIGINS contains a localhost origin in production. "
            "Set it to your deployed frontend URL only."
        )

    # FRONTEND_BASE_URL drives the Slack OAuth callback redirect.
    # Without it the callback bounces users into CORS_ORIGINS[0],
    # which is fragile in production.
    if not (os.getenv("FRONTEND_BASE_URL") or "").strip():
        issues.append(
            "FRONTEND_BASE_URL is not set. In production this is "
            "required so the Slack OAuth callback redirects users "
            "back to your deployed frontend."
        )

    # SLACK_REDIRECT_URI must be HTTPS in production -- Slack rejects
    # http redirects against production apps anyway, but this is a
    # nicer error than the one Slack will give you.
    slack_redirect = (os.getenv("SLACK_REDIRECT_URI") or "").strip()
    if slack_redirect and not slack_redirect.lower().startswith("https://"):
        issues.append(
            "SLACK_REDIRECT_URI must be HTTPS in production "
            f"(got: {slack_redirect})."
        )

    # Secrets that still look like .env.example placeholders.
    sensitive = (
        "APP_API_KEY",
        "SLACK_OAUTH_STATE_SECRET",
        "SUPABASE_JWT_SECRET",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SLACK_CLIENT_SECRET",
        "SLACK_SIGNING_SECRET",
        "HYDRADB_API_KEY",
        "OPENAI_API_KEY",
    )
    for name in sensitive:
        if _looks_like_placeholder((os.getenv(name) or "").strip()):
            issues.append(
                f"{name} still looks like a placeholder value. "
                "Replace it with a real secret before deploying."
            )

    return issues


def _audit_secrets() -> None:
    """
    Emit a REDACTED summary of which secrets are present, so an
    operator can confirm at deploy time that env vars made it through.

    We never log the values themselves -- only their length and a
    fingerprint (first 4 chars, last 4 chars). Long enough to spot a
    "did I paste the wrong key" mistake; short enough that the log
    line isn't itself a secret.
    """
    audited = (
        "APP_API_KEY",
        "HYDRADB_API_KEY",
        "OPENAI_API_KEY",
        "SUPABASE_JWT_SECRET",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SLACK_CLIENT_SECRET",
        "SLACK_SIGNING_SECRET",
        "SLACK_OAUTH_STATE_SECRET",
    )
    summary = {}
    for name in audited:
        value = (os.getenv(name) or "").strip()
        if not value:
            summary[name] = "missing"
            continue
        n = len(value)
        if n <= 8:
            fingerprint = "***"
        else:
            fingerprint = f"{value[:4]}...{value[-4:]}"
        summary[name] = f"len={n} fp={fingerprint}"
        if _looks_like_placeholder(value):
            summary[name] += " (PLACEHOLDER)"
    logger.info("secrets_audit", extra=summary)


def validate_required_env() -> None:
    """
    Verify the env. Raises StartupConfigError on any missing required
    var or (in production) any failed production-mode sanity check.

    Also emits a redacted secrets-audit log entry so operators can
    confirm the env wired through correctly.
    """
    missing = _validate_required_env()
    if missing:
        lines = [
            "",
            "=" * 64,
            "Second Brain backend cannot start.",
            "",
            "The following required environment variables are missing or blank:",
        ]
        for name in missing:
            lines.append(f"  - {name}")
        lines.extend([
            "",
            "Fix:",
            "  1. Copy backend/.env.example to backend/.env if you haven't.",
            "  2. Fill in the values for the variables above.",
            "  3. Restart the server.",
            "",
            "If you are using OpenRouter / Together / Groq / Azure-compatible,",
            "set OPENAI_API_KEY to the provider's key and OPENAI_BASE_URL to",
            "their endpoint.",
            "=" * 64,
            "",
        ])
        raise StartupConfigError("\n".join(lines))

    # Gmail is opt-in. If the user set SOME Gmail vars but not all,
    # fail fast at boot rather than at first /api/gmail/connect-url
    # click. This runs in BOTH local and production modes -- the
    # foot-gun exists in either.
    gmail_partial = _validate_gmail_group()
    if gmail_partial:
        lines = [
            "",
            "=" * 64,
            "Second Brain backend cannot start.",
            "",
            "Gmail Connect is partially configured. Either set ALL four of",
            "the Gmail OAuth environment variables, or unset all of them",
            "to disable the Gmail connector.",
            "",
            "Currently missing or blank:",
        ]
        for name in gmail_partial:
            lines.append(f"  - {name}")
        lines.extend([
            "",
            "See backend/.env.example for descriptions of each var.",
            "=" * 64,
            "",
        ])
        raise StartupConfigError("\n".join(lines))

    _audit_secrets()

    if _is_production():
        prod_issues = _validate_production_env()
        if prod_issues:
            lines = [
                "",
                "=" * 64,
                "Production config check failed (ENVIRONMENT=production).",
                "",
                "Issues:",
            ]
            for issue in prod_issues:
                lines.append(f"  - {issue}")
            lines.extend([
                "",
                "Set ENVIRONMENT=local or fix the issues above.",
                "=" * 64,
                "",
            ])
            raise StartupConfigError("\n".join(lines))
        logger.info("startup_env_validated", extra={"mode": "production"})
    else:
        logger.info("startup_env_validated", extra={"mode": "non_production"})