"""
Startup-time environment validation for the Second Brain MVP.

Called from main.py's FastAPI lifespan. If anything required is missing
or blank, we raise StartupConfigError with a clear, multi-line message
so uvicorn prints it and exits — better than letting requests fail
mysteriously at 3am.
"""

import os
from typing import List

from logging_config import get_logger

logger = get_logger(__name__)


# Vars whose values are required to be set AND non-empty before the app
# can serve any request. Optional/tunable vars (LLM_MAX_TOKENS,
# DEBUG_RECALL, CORS_ORIGINS, etc.) are NOT validated here — they have
# safe defaults.
REQUIRED_ENV_VARS = (
    "APP_API_KEY",
    "HYDRADB_API_KEY",
    "HYDRADB_TENANT_ID",
    "OPENAI_API_KEY",  # also used for OpenRouter and other OpenAI-compatible providers
)


class StartupConfigError(RuntimeError):
    """Raised when required configuration is missing at startup."""


def validate_required_env() -> None:
    """
    Verify every entry in REQUIRED_ENV_VARS is set and non-blank.
    Raise StartupConfigError with a list of the missing ones.
    """
    missing: List[str] = [
        name for name in REQUIRED_ENV_VARS
        if not (os.getenv(name) or "").strip()
    ]
    if not missing:
        logger.info('startup_env_validated')
        return

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