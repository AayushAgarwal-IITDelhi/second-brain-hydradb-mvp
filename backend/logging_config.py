"""
Structured JSON logging for the Second Brain backend.

Zero first-party imports — this module must be importable before any other
backend module so it can be used in all of them.

ContextVars carry per-request state across async tasks and threads:
  request_id     -- UUID generated per HTTP request
  correlation_id -- X-Correlation-ID from client (falls back to request_id)
  user_id        -- reserved for future auth; always null today
  workspace_id   -- reserved for future auth; always null today
"""

import json
import logging
import traceback
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------- #
# Per-request context (read at log-emit time so async tasks see their own
# values even when a background thread emits while handling another request)
# ---------------------------------------------------------------------- #
_request_id: ContextVar[Optional[str]] = ContextVar('request_id', default=None)
_correlation_id: ContextVar[Optional[str]] = ContextVar('correlation_id', default=None)
_user_id: ContextVar[Optional[str]] = ContextVar('user_id', default=None)
_workspace_id: ContextVar[Optional[str]] = ContextVar('workspace_id', default=None)


# ---------------------------------------------------------------------- #
# JSON formatter
# ---------------------------------------------------------------------- #
_STDLIB_ATTRS = frozenset({
    'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
    'funcName', 'levelname', 'levelno', 'lineno', 'message', 'module',
    'msecs', 'msg', 'name', 'pathname', 'process', 'processName',
    'relativeCreated', 'stack_info', 'taskName', 'thread', 'threadName',
})


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()

        entry = {
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            'level': record.levelname,
            'service': 'second-brain-backend',
            'module': record.module,
            'event': message,
            'message': message,
            'request_id': _request_id.get(),
            'correlation_id': _correlation_id.get(),
            'user_id': _user_id.get(),
            'workspace_id': _workspace_id.get(),
        }

        extra = {
            k: v for k, v in record.__dict__.items()
            if k not in _STDLIB_ATTRS and not k.startswith('_')
        }
        if extra:
            entry['extra'] = extra

        if record.exc_info:
            entry['exception'] = self.formatException(record.exc_info)
        elif record.exc_text:
            entry['exception'] = record.exc_text

        return json.dumps(entry, default=str)


# ---------------------------------------------------------------------- #
# Public API
# ---------------------------------------------------------------------- #
def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def configure_logging(level: str = 'INFO') -> None:
    """
    Configure the root logger with the JSON formatter. Idempotent — calling
    this more than once will not add duplicate handlers.
    """
    root = logging.getLogger()
    if any(isinstance(h, logging.StreamHandler) and isinstance(getattr(h, 'formatter', None), _JsonFormatter)
           for h in root.handlers):
        return  # already configured

    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Quiet down noisy third-party loggers that we don't need at INFO+.
    for noisy in ('uvicorn.access', 'apscheduler', 'slack_sdk'):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def bind_request_context(request_id: str, correlation_id: Optional[str] = None) -> None:
    _request_id.set(request_id)
    _correlation_id.set(correlation_id or request_id)


def bind_user_context(user_id: Optional[str], workspace_id: Optional[str] = None) -> None:
    """Future auth slot. Call from auth middleware after token verification."""
    _user_id.set(user_id)
    _workspace_id.set(workspace_id)
