# Retry / Backoff Framework

Generic retry-with-exponential-backoff for all external service calls.

---

## Strategy

### Exponential backoff

```
attempt 1 fails → sleep 1 s
attempt 2 fails → sleep 2 s
attempt 3 fails → raise RetryExhausted
```

Base formula: `delay = min(initial_delay × multiplier^(attempt-1), max_delay)`

### Jitter (enabled by default)

Each computed delay is multiplied by a random factor in **[0.75, 1.25]** to
spread simultaneous retries after coordinated restarts.

Disable with `jitter=False` (useful in tests for deterministic timing).

---

## Retryable Conditions

### HTTP status codes (always retried)

| Code | Reason |
|------|--------|
| `429` | Rate-limited — back off and retry |
| `500` | Internal Server Error — transient server fault |
| `502` | Bad Gateway — upstream proxy error |
| `503` | Service Unavailable — overloaded / deploying |
| `504` | Gateway Timeout — upstream timed out |

### Exception types (default retryable list)

- `TimeoutError`
- `ConnectionError`
- `OSError`

Additional types can be passed per-call (see examples below).

### Never retried

| Condition | Reason |
|-----------|--------|
| HTTP `400` | Bad request — fix the payload |
| HTTP `401` | Unauthorized — invalid credentials; retrying won't help |
| HTTP `403` | Forbidden — permission denied |
| HTTP `404` | Not found |
| HTTP `422` | Validation error |
| `ValueError` / `TypeError` | Programming mistake, not transient |
| `NonRetryableError` | Explicit marker to stop retry immediately |
| `non_retryable_exceptions` param | Caller-specified stop list |

---

## Usage

### Basic decorator

```python
from retry import retry

@retry(service="hydradb", max_attempts=3)
def upload_to_hydradb(files):
    ...
```

```python
@retry(service="llm", max_attempts=3)
async def call_llm(**kwargs):
    ...
```

### Custom parameters

```python
@retry(
    service="myservice",
    max_attempts=5,
    initial_delay=0.5,
    max_delay=60.0,
    exponential_multiplier=3.0,
    jitter=True,
    retryable_exceptions=(requests.Timeout, requests.ConnectionError),
    retryable_status_codes=(429, 503),
)
def call_external_api():
    ...
```

### Functional (no-decorator) form

```python
from retry import retry

_wrapped = retry(service="batch", max_attempts=3)(original_function)
result = _wrapped(arg1, arg2)
```

### Stopping retries from inside a function

```python
from retry import NonRetryableError

@retry(service="myservice", max_attempts=3)
def upload(payload):
    if not payload.get("id"):
        raise NonRetryableError("missing id — bad payload")
    ...
```

---

## AppError integration

`AppError` subclasses (e.g. `HydraDBError`, `LLMError`) accept an
`upstream_status` keyword argument that carries the HTTP status returned by
the upstream service (separate from the app's own response code).

The retry framework inspects `upstream_status` to decide whether to retry:

```python
raise HydraDBError(
    detail="Knowledge backend error",
    upstream_status=503,   # ← retried
)

raise HydraDBError(
    detail="Authentication failed",
    upstream_status=401,   # ← NOT retried
)
```

---

## Where retry is applied

| Service | Scope | Config |
|---------|-------|--------|
| **HydraDB** `full_recall` | Whole method | 3 attempts, 1 s initial |
| **HydraDB** `upload_knowledge` | Inner `requests.post` | 3 attempts, 1 s initial |
| **LLM** `_create_completion` | OpenAI SDK call | 3 attempts, 1 s initial |
| **Slack** page fetches | Per-page API call | 3 attempts, 1 s initial |
| **Slack** user/permalink lookups | Per lookup | 3 attempts, 1 s initial |
| **Scheduler** ingestion | Whole ingestion run | 3 attempts, 5 s initial |

### Slack rate-limit handling

Slack 429s are handled separately by the existing pagination loop in
`SlackClientWrapper` (which reads and respects the `Retry-After` header).
The `retry` decorator on Slack calls additionally covers **network-level**
failures (`ConnectionError`, `TimeoutError`, `OSError`).

### Streaming restriction

The `retry` decorator **must only wrap the initialisation call** — the
point before the first token is emitted by a streaming response.

- ✅ Apply `@retry` to the function that *opens* the stream.
- ❌ Do NOT apply `@retry` to a generator already yielding tokens.
  Restarting mid-stream produces garbled / duplicate output.

The current LLM path (`generate_grounded_answer`) is non-streaming, so this
restriction is not yet active; it is documented for future streaming work.

---

## Structured logs

Every retry event emits a JSON line to stdout:

```json
{"event": "retry_attempt",   "service": "hydradb", "attempt": 1, "delay_seconds": 1.0, "error": "..."}
{"event": "retry_success",   "service": "hydradb", "attempt": 2}
{"event": "retry_exhausted", "service": "hydradb", "attempt": 3, "error": "..."}
```

No log is emitted when the first attempt succeeds.

---

## Default parameters

| Parameter | Default | Notes |
|-----------|---------|-------|
| `max_attempts` | `3` | Total attempts (not retries) |
| `initial_delay` | `1.0` s | Delay after first failure |
| `max_delay` | `30.0` s | Upper cap |
| `exponential_multiplier` | `2.0` | Doubles each time |
| `jitter` | `True` | ±25 % random variance |
| `service` | `"unknown"` | Name used in log lines |

---

## Adding retry to a new service

```python
from retry import retry

@retry(
    service="supabase",
    max_attempts=3,
    retryable_exceptions=(ConnectionError, TimeoutError),
)
async def query_supabase(sql: str):
    ...
```

The framework handles both sync and async functions — no changes needed
to the decorator call signature.
