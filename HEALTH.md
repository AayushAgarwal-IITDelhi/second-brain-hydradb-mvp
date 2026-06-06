# Health Check System

Production-grade health endpoints for the Second Brain MVP.

---

## Endpoints

### `GET /api/health/live` — Liveness

**Purpose:** Is the process running?

Returns `200` while the process is alive. Never fails unless the OS/container has killed the process.

Use this for Kubernetes `livenessProbe` or any "restart if down" probe.

```json
{ "status": "alive" }
```

---

### `GET /api/health/ready` — Readiness

**Purpose:** Can the app serve user traffic right now?

Checks only **required** dependencies. Returns `200` if all required services are healthy, `503` otherwise.

Use this for Kubernetes `readinessProbe` or load-balancer health checks.

**200 — ready**
```json
{
  "status": "ready",
  "checks": {
    "hydradb": "ok",
    "llm": "ok"
  }
}
```

**503 — not ready**
```json
{
  "status": "not_ready",
  "checks": {
    "hydradb": "error",
    "llm": "ok"
  }
}
```

---

### `GET /api/health` — Detailed Diagnostics

**Purpose:** Full operational snapshot. Always returns `200`; inspect `"status"` in the body.

Checks all registered services (required + optional) with latency measurements.

**Response shape**
```json
{
  "status": "degraded",
  "timestamp": "2026-05-19T10:30:00.000000+00:00",
  "checks": {
    "hydradb": {
      "status": "ok",
      "latency_ms": 118.3
    },
    "llm": {
      "status": "ok",
      "latency_ms": 241.7
    },
    "slack": {
      "status": "warning",
      "latency_ms": 95.0,
      "message": "slack error: invalid_auth"
    },
    "scheduler": {
      "status": "ok",
      "jobs": 1
    },
    "database": {
      "status": "not_configured"
    }
  }
}
```

**Overall `status` values**

| Value | Meaning |
|-------|---------|
| `healthy` | All registered checks passed |
| `degraded` | All **required** checks passed; one or more **optional** checks warn/error |
| `unhealthy` | One or more **required** checks returned an error |

---

## Dependency Model

| Service | Type | Required |
|---------|------|----------|
| HydraDB | Knowledge backend | ✅ Required |
| LLM provider | Answer generation | ✅ Required |
| Slack API | Ingestion source | Optional |
| Scheduler | Background ingestion | Optional |
| Database | Future (Supabase) | Optional — placeholder |

**Required** services: failure → readiness returns 503 and overall `"unhealthy"`.

**Optional** services: failure → overall `"degraded"` but readiness still passes.

---

## Per-Check Behaviour

### HydraDB
- Sends a minimal `POST /recall/full_recall` with `query: "health check"` and `top_k: 1`
- Measures round-trip latency
- Auth failures (`401`/`403`) → `error`
- 5xx → `error`
- Network timeout → `error`

### LLM
- Sends `POST /chat/completions` with `max_tokens: 1` (cheapest possible call)
- Auth failure → `error`
- Connection error → `error`

### Slack
- Calls `auth.test` (free endpoint, validates token + workspace)
- Token missing (`SLACK_BOT_TOKEN` not set) → `not_configured` (neutral)
- Token invalid → `warning`

### Scheduler
- Introspects the live APScheduler instance (`scheduler._scheduler`)
- Disabled (`AUTO_INGEST=false`) → `ok` with `"jobs": 0`
- Enabled but not running → `warning`
- Running → `ok` + job count

### Database (placeholder)
- Always returns `not_configured`
- Exists so future Supabase support drops in without touching the endpoint logic

---

## Timeout Protection

Every individual check is capped at **3 seconds** (`CHECK_TIMEOUT_SECONDS`).

A hung external service cannot hang the endpoint. The check returns `status: "error"` with `"message": "check timed out after 3.0s"`.

---

## Structured Logs

Each check emits three structured JSON lines to stdout:

```json
{"event": "health_check_started",   "check": "hydradb"}
{"event": "health_check_completed", "check": "hydradb", "status": "ok"}
{"event": "health_check_failed",    "check": "hydradb", "reason": "timed out after 3.0s"}
```

---

## Adding a Future Service

1. Create a class in `health.py` (or your own module) that inherits `HealthCheck`:

```python
from health import HealthCheck, HealthResult, STATUS_OK, STATUS_ERROR

class SupabaseHealthCheck(HealthCheck):
    name = "database"      # overrides the placeholder
    required = False       # True if traffic can't be served without it

    async def check(self) -> HealthResult:
        import time
        start = time.monotonic()
        try:
            # ... lightweight Supabase ping ...
            return HealthResult(
                status=STATUS_OK,
                latency_ms=(time.monotonic() - start) * 1000,
            )
        except Exception as exc:
            return HealthResult(status=STATUS_ERROR, message=str(exc))
```

2. Register it **before the app starts serving** (e.g. in `main.py` or a startup hook):

```python
from health import register_health_check
register_health_check(SupabaseHealthCheck())
```

The new check will automatically appear in all three endpoints.
If you want it to affect readiness, set `required = True`.
