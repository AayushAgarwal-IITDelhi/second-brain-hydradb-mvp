"""
Tests for logging_config.py.

Verifies JSON shape, ContextVar isolation, idempotency, extra fields, and
that known sensitive field names never appear in log output.
"""

import json
import logging
import sys
import threading
from io import StringIO

import pytest

import logging_config
from logging_config import (
    _JsonFormatter,
    _request_id,
    _correlation_id,
    bind_request_context,
    bind_user_context,
    configure_logging,
    get_logger,
)


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _capture_record(message: str, level=logging.INFO, **extra) -> dict:
    """Emit one log record through _JsonFormatter and return the parsed JSON."""
    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name='test',
        level=level,
        pathname='',
        lineno=0,
        msg=message,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    line = formatter.format(record)
    return json.loads(line)


# ---------------------------------------------------------------------- #
# JSON shape
# ---------------------------------------------------------------------- #
REQUIRED_KEYS = {
    'timestamp', 'level', 'service', 'module', 'event', 'message',
    'request_id', 'correlation_id', 'user_id', 'workspace_id',
}


def test_json_has_all_required_keys():
    out = _capture_record('test_event')
    assert REQUIRED_KEYS.issubset(out.keys())


def test_service_field():
    out = _capture_record('x')
    assert out['service'] == 'second-brain-backend'


def test_level_field():
    out = _capture_record('x', level=logging.WARNING)
    assert out['level'] == 'WARNING'


def test_event_and_message_match():
    out = _capture_record('my_event')
    assert out['event'] == 'my_event'
    assert out['message'] == 'my_event'


def test_timestamp_format():
    out = _capture_record('x')
    ts = out['timestamp']
    assert ts.endswith('Z')
    assert 'T' in ts


def test_null_context_by_default():
    """Fresh ContextVars should produce null fields in the output."""
    _request_id.set(None)
    _correlation_id.set(None)
    out = _capture_record('x')
    assert out['request_id'] is None
    assert out['correlation_id'] is None
    assert out['user_id'] is None
    assert out['workspace_id'] is None


# ---------------------------------------------------------------------- #
# Extra fields
# ---------------------------------------------------------------------- #
def test_extra_dict_present():
    out = _capture_record('event', chunks=5, mode='default')
    assert 'extra' in out
    assert out['extra']['chunks'] == 5
    assert out['extra']['mode'] == 'default'


def test_no_extra_key_when_empty():
    out = _capture_record('plain_event')
    assert 'extra' not in out


# ---------------------------------------------------------------------- #
# ContextVar propagation
# ---------------------------------------------------------------------- #
def test_bind_request_context():
    bind_request_context('req-123', 'corr-456')
    out = _capture_record('ctx')
    assert out['request_id'] == 'req-123'
    assert out['correlation_id'] == 'corr-456'


def test_bind_request_context_correlation_fallback():
    bind_request_context('req-abc')
    out = _capture_record('ctx')
    assert out['request_id'] == 'req-abc'
    assert out['correlation_id'] == 'req-abc'


def test_bind_user_context():
    bind_user_context('user-1', 'ws-2')
    out = _capture_record('ctx')
    assert out['user_id'] == 'user-1'
    assert out['workspace_id'] == 'ws-2'


def test_context_isolation_across_threads():
    """Two threads must see independent request_ids."""
    results = {}

    def worker(thread_name: str, req_id: str) -> None:
        bind_request_context(req_id)
        import time
        time.sleep(0.05)
        out = _capture_record('isolation')
        results[thread_name] = out['request_id']

    t1 = threading.Thread(target=worker, args=('t1', 'id-thread-1'))
    t2 = threading.Thread(target=worker, args=('t2', 'id-thread-2'))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results['t1'] == 'id-thread-1'
    assert results['t2'] == 'id-thread-2'


# ---------------------------------------------------------------------- #
# Exception serialisation
# ---------------------------------------------------------------------- #
def test_exception_field_present():
    formatter = _JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name='test', level=logging.ERROR, pathname='', lineno=0,
            msg='err', args=(), exc_info=sys.exc_info(),
        )
    line = formatter.format(record)
    out = json.loads(line)
    assert 'exception' in out
    assert 'ValueError' in out['exception']


# ---------------------------------------------------------------------- #
# No-secret check
# ---------------------------------------------------------------------- #
SENSITIVE_FIELDS = ('api_key', 'token', 'password', 'secret', 'OPENAI_API_KEY')


def test_no_sensitive_fields_in_output():
    out = _capture_record('safe_event', chunks=5, mode='test')
    serialized = json.dumps(out)
    for field in SENSITIVE_FIELDS:
        assert field not in serialized


# ---------------------------------------------------------------------- #
# Idempotency
# ---------------------------------------------------------------------- #
def test_configure_logging_idempotent():
    """Calling configure_logging twice must not add a second handler."""
    root = logging.getLogger()
    initial_count = len(root.handlers)

    configure_logging()
    configure_logging()

    json_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and isinstance(getattr(h, 'formatter', None), _JsonFormatter)
    ]
    assert len(json_handlers) == 1


def test_get_logger_returns_logger():
    lg = get_logger('mymodule')
    assert isinstance(lg, logging.Logger)
    assert lg.name == 'mymodule'
