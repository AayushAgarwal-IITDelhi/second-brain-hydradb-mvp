# Structured Logging

## Architecture

```
logging_config.py        (stdlib only — zero first-party imports)
  ↑ imported by:
request_context.py       (raw ASGI middleware)
errors.py
startup.py
  ↑ imported by:
hydradb_client.py
llm.py
  ↑ imported by:
recall.py  scheduler.py  realtime_ingest.py  ingestion/*.py
  ↑ imported by:
main.py                  (calls configure_logging() at import time)
```

## Request flow

```
HTTP request arrives
  → RequestContextMiddleware.__call__
      generate UUID request_id
      read X-Correlation-ID from headers (fallback: request_id)
      bind_request_context(request_id, correlation_id)
      → your endpoint runs
          every logger.X(...) call reads ContextVars at emit time
          → JSON line includes request_id + correlation_id
      wrap send() to inject X-Request-ID on http.response.start
HTTP response sent
```

## Log schema

Every log line is a single JSON object:

```json
{
  "timestamp":      "2026-05-19T12:34:56.789Z",
  "level":          "INFO",
  "service":        "second-brain-backend",
  "module":         "recall",
  "event":          "recall_context_ready",
  "message":        "recall_context_ready",
  "request_id":     "550e8400-e29b-41d4-a716-446655440000",
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id":        null,
  "workspace_id":   null,
  "extra": {
    "chunks_count": 5,
    "mode": "default"
  }
}
```

| Field          | Type             | Notes                                                    |
|----------------|------------------|----------------------------------------------------------|
| timestamp      | ISO-8601 + Z     | UTC, millisecond precision                               |
| level          | string           | DEBUG / INFO / WARNING / ERROR                           |
| service        | string           | Always `second-brain-backend`                            |
| module         | string           | Python module name (`record.module`)                     |
| event          | string           | Stable snake_case event name (use for filtering/alerting)|
| message        | string           | Same as event (duplicate for log-parser compatibility)   |
| request_id     | UUID string\|null | Generated per HTTP request; null outside request context |
| correlation_id | UUID string\|null | From X-Correlation-ID header; falls back to request_id  |
| user_id        | null             | Reserved for future auth — always null today             |
| workspace_id   | null             | Reserved for future auth — always null today             |
| extra          | object\|absent   | Caller-supplied structured fields                        |
| exception      | string\|absent   | Formatted traceback (only on exc_info=True calls)        |

## What is and isn't logged

**SAFE to log:** event names, counts, channel_ids, stable_keys, HTTP status codes,
durations, cache hit/miss, message_length (int), model name, mode, top_k, error class names.

**NEVER log:** API keys, tokens, Slack message text, user names, full question/context
strings, raw HydraDB response bodies.

## Filtering examples

```bash
# All log lines for a single request
LOG_FILE=app.log grep '"request_id": "your-uuid"' "$LOG_FILE"

# All warnings and errors
grep '"level": "WARNING"\|"level": "ERROR"' app.log

# Scheduler events only
grep '"module": "scheduler"' app.log

# Specific event
grep '"event": "hydradb_upload_batch_summary"' app.log | jq '.extra'
```

## Future auth integration

When Supabase auth (or any JWT middleware) is added:

1. Auth middleware verifies the token and extracts `user_id` + `workspace_id`.
2. Call `bind_user_context(user_id, workspace_id)` immediately after verification.
3. Every subsequent log line in that request automatically includes both fields.
4. **No changes needed in any business-logic module** — the ContextVars propagate automatically.

```python
# In your future auth middleware (pseudo-code):
from logging_config import bind_user_context

async def auth_middleware(request, call_next):
    claims = verify_jwt(request.headers.get("Authorization"))
    bind_user_context(user_id=claims["sub"], workspace_id=claims["workspace_id"])
    return await call_next(request)
```

## Configuring log level

Set `LOG_LEVEL` in your `.env`:

```
LOG_LEVEL=DEBUG   # verbose — includes HydraDB request/response details
LOG_LEVEL=INFO    # default — lifecycle events, upload summaries, job start/finish
LOG_LEVEL=WARNING # quiet — only failures and unexpected states
```
