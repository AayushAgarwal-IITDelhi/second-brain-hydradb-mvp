# Implementation Audit Report — Second Brain MVP

**Audited by:** Senior Code Review  
**Date:** 2026-05-19  
**Branch:** `main` (HEAD: `609741a`)  
**Scope:** pytest suite · GitHub Actions CI · structured logging · health endpoints · retry/backoff

---

## Executive Summary

**Overall Score: 0.8 / 10**

**Production Readiness: HIGH RISK — Do not ship**

The five features under audit are in one of three states:

| State | Features |
|-------|----------|
| **Deleted from HEAD** | pytest suite, GitHub Actions CI |
| **Not implemented** (print-only / hardcoded stub) | structured logging, health endpoints |
| **Framework unmerged, active bug present** | retry/backoff |

A critical discovery explains the gap: sophisticated implementations of health endpoints, retry/backoff, and structured logging were developed on an `aayush_health` feature branch but **were never merged into `main`**. Meanwhile, the previously-existing pytest suite (28 files) and CI workflows were **deleted in commit `609741a`** without replacement.

The codebase currently has:
- **Zero automated tests**
- **Zero CI gates** — code can be pushed without any quality check
- **Zero structured logs** — 50+ bare `print()` calls
- **A hardcoded health endpoint** that always returns `"ok"` regardless of actual system state
- **An infinite retry loop** in the Slack ingestion client that can hang the ingestion process indefinitely

---

## Feature Audit

---

### 1. pytest Suite

**Status: DELETED**  
**Score: 0 / 10**

#### A. Implementation Exists?
**No.** `backend/tests/` does not exist in HEAD. The directory and all 28 test files were removed in commit `609741a`.

#### B. Correctly Integrated?
N/A — nothing to integrate.

#### C. Actually Used?
Running `pytest` from `backend/` finds zero tests and exits 0 (no error, no output). This means CI would also silently pass even if re-added with the deleted workflows.

#### D. Production-Ready?
No.

#### E. Gaps / Issues

**What existed (git history, commit `7aeb6e6` and earlier):**

```
backend/tests/
├── conftest.py                        186 lines — comprehensive fixtures
├── test_api.py                        442 lines
├── test_auth.py                        80 lines
├── test_health.py                     627 lines
├── test_retry.py                      569 lines
├── test_recall.py                     150+ lines
├── test_rate_limit.py                 100+ lines
├── test_hydradb_client.py
├── test_llm.py
├── test_logging.py
├── test_query_rewriter.py
├── test_request_context.py
├── test_scheduler.py
├── test_search_utils.py
├── test_slack_client.py
├── test_slack_signature.py
├── test_startup.py
├── test_cache.py
├── test_date_utils.py
├── test_errors.py
├── test_recall_integration.py
├── ingestion/
│   ├── test_ingest_slack.py           368 lines
│   ├── test_ingestion_state.py        208 lines
│   └── test_normalize.py             195 lines
└── integration/
    ├── test_query_flow.py             270 lines
    ├── test_streaming.py             275 lines
    └── test_ingestion_pipeline.py    257 lines
```

**Strengths of the deleted suite:**
- `conftest.py` patched all env vars before module import (correct for modules that read config at load time)
- Lifecycle hooks (`startup.validate_required_env`, `scheduler.start_scheduler`) properly mocked
- `pytest-asyncio` configured with `asyncio_mode = auto` in `setup.cfg`
- Coverage threshold: 85% (`--cov-fail-under=85`)
- Parametrized tests for validation edge cases
- Integration tests for query flow, streaming, ingestion pipeline

**Known bugs in the deleted suite:**
- `test_api.py:370` patches `realtime_ingest.process_slack_event` — module does not exist in source; test would fail on import
- CI yaml was missing env vars that conftest.py required (`HYDRADB_SUB_TENANT_ID`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_IDS`)

**Strengths:** The historical test suite was well-structured and comprehensive.  
**Weaknesses:** It was deleted without replacement.  
**Missing:** All of it — the codebase has zero test coverage today.

---

### 2. GitHub Actions CI

**Status: DELETED**  
**Score: 0 / 10**

#### A. Implementation Exists?
**No.** `.github/workflows/` does not exist in HEAD. Both `ci.yml` and `quality.yml` were removed in commit `609741a`.

#### B. Correctly Integrated?
N/A.

#### C. Actually Used?
No CI runs. Code merged to `main` undergoes zero automated checks.

#### D. Production-Ready?
No.

#### E. Gaps / Issues

**What existed (git history, commit `a140431` and `00713c6`):**

**`ci.yml` — 3 jobs:**
1. `backend-tests`: Python 3.11, pip cache, `pytest tests/ --cov=. --cov-report=term-missing --cov-fail-under=85`
2. `frontend-tests`: Node 20, `npm ci` + `npm run build` + optional lint
3. `smoke-validation`: imports Python modules to verify they load

**`quality.yml` — 2 jobs:**
1. `backend-quality`: black + isort + flake8
2. `frontend-quality`: optional `npm run lint`

**Bugs in the deleted workflows:**

| Bug | Severity | Detail |
|-----|----------|--------|
| Smoke validation imports `logging_config`, `request_context`, `realtime_ingest` | Critical | None of these exist in current HEAD — smoke job would fail immediately |
| Smoke validation checks for `backend/.env.example` | Critical | File does not exist in repo |
| `black` has `continue-on-error: true` | Medium | Formatting failures never block merges |
| `isort` has `continue-on-error: true` | Medium | Import-order failures never block merges |
| CI missing env vars vs conftest expectations | Medium | `HYDRADB_SUB_TENANT_ID`, `SLACK_*` vars absent from CI yaml |
| No frontend test execution | Low | Only build + optional lint; `npm test` never called |

**Strengths:** Triggers correctly on push + PR to `main`/`develop`. Pip and npm caching present.  
**Weaknesses:** Quality gates are partially disabled; smoke validation references phantom modules.  
**Missing:** The entire `.github/` directory.

---

### 3. Structured Logging

**Status: NOT IMPLEMENTED**  
**Score: 1 / 10**

#### A. Implementation Exists?
**No.** Neither `logging_config.py` nor `request_context.py` exist in `backend/`. The Python `logging` module is not imported anywhere in the codebase.

#### B. Correctly Integrated?
N/A — nothing exists to integrate.

#### C. Actually Used?
The codebase uses exclusively `print()` statements. 50+ calls across 12 files.

#### D. Production-Ready?
No. Plain `print()` to stdout:
- Cannot be parsed by log aggregators (ELK, Datadog, CloudWatch)
- Carries no timestamp, severity level, or machine-readable fields
- Cannot be filtered, searched, or alerted on
- Cannot trace a request across module boundaries

#### E. Gaps / Issues

**Print call inventory:**

| File | Count | Notable Content |
|------|-------|-----------------|
| `ingestion/ingest_slack.py` | 16 | Channel IDs, message counts, batch sizes |
| `hydradb_client.py` | 10 | **Full HTTP response body** (line 196), response body in error path (line 293) |
| `recall.py` | 7 | Raw HydraDB response + Slack content (gated by `DEBUG_RECALL=true`) |
| `scheduler.py` | 7 | `traceback.print_exc()` (multi-line, unparseable) |
| `ingestion/slack_client.py` | 6 | Slack API errors |
| `errors.py` | 2 | Exception handler — no timestamp, path, or status code |
| `ingestion/ingestion_state.py` | 1 | State file errors |
| `main.py` | 1 | CORS config on startup |

**Critical issues:**

- **`hydradb_client.py:196`** — logs `response.text` (full body) on every successful POST. If HydraDB returns verbose error messages containing internal details, they go to stdout unredacted.
- **`hydradb_client.py:293`** — error path logs `response.text[:400]`. Same risk.
- **`scheduler.py:70`** — `traceback.print_exc()` produces multi-line output; log aggregators treat each line as a separate event.
- **`errors.py:79-81`** — the global exception handler (`app_error_handler`) has no request path, HTTP method, or response status in its output.
- **No request/correlation IDs** — there is no way to associate a log line in `hydradb_client.py` with the originating HTTP request in `main.py`.
- **No middleware** — no HTTP request/response logging for endpoint path, method, status, or latency.
- **No `user_id`/`workspace_id` fields** — blocks any future multi-tenant log filtering.

**Only safe behavior:** API keys and auth tokens are not directly printed. `DEBUG_RECALL` gate (off by default) prevents Slack message content from appearing in normal operation.

**What was designed (not merged, `aayush_health` branch):**
- `backend/logging_config.py` — JSON formatter, configurable log level, module-scoped loggers
- `backend/request_context.py` — UUID request ID generation, FastAPI middleware for propagation
- Used by both `health.py` and `retry.py` on that branch

**Strengths:** Defensive coding avoids credential exposure.  
**Weaknesses:** Zero structured logging infrastructure; `print()` throughout.  
**Missing:** `logging_config.py`, `request_context.py`, request middleware, correlation IDs, JSON formatter, all log-level gates.

---

### 4. Health Endpoints

**Status: HARDCODED STUB — Missing liveness/readiness probes**  
**Score: 1 / 10**

#### A. Implementation Exists?
Partially. One endpoint exists: `GET /api/health`. The liveness and readiness probes do not exist.

#### B. Correctly Integrated?
The endpoint is registered and reachable. However it provides no useful information.

#### C. Actually Used?
The endpoint exists and is unauthenticated (correct). It returns `{"status": "ok", "service": "second-brain-api"}` unconditionally.

#### D. Production-Ready?
No. A container orchestrator using this endpoint as a health probe would report the pod as healthy even if HydraDB is unreachable, the LLM key is revoked, or the scheduler has crashed.

#### E. Gaps / Issues

**Current implementation (`main.py:137-140`):**
```python
@app.get("/api/health")
def health() -> Dict[str, str]:
    """Public health check so external probes don't need the API key."""
    return {"status": "ok", "service": "second-brain-api"}
```

**Checklist:**

| Check | Result |
|-------|--------|
| `GET /api/health/live` exists | ✗ Missing |
| `GET /api/health/ready` exists | ✗ Missing |
| `GET /api/health` exists | ✓ Present |
| HydraDB connectivity checked | ✗ No |
| LLM connectivity checked | ✗ No |
| Slack token checked | ✗ No |
| Scheduler state checked | ✗ No |
| Timeout enforced per check | ✗ No |
| Status can be "degraded" | ✗ No |
| Status can be "unhealthy" | ✗ No |
| Endpoint is unauthenticated | ✓ Correct |

**Mock failure scenarios and actual responses:**

| Scenario | Expected | Actual |
|----------|----------|--------|
| HydraDB down | `503 unhealthy` | `200 {"status":"ok"}` |
| OpenAI key revoked | `503 degraded` | `200 {"status":"ok"}` |
| Slack token expired | `200 degraded` | `200 {"status":"ok"}` |
| Scheduler crashed | `200 degraded` | `200 {"status":"ok"}` |
| Everything down | `503 unhealthy` | `200 {"status":"ok"}` |

**What was designed (not merged, `aayush_health` branch, `backend/health.py`, 478 lines):**
- `GET /api/health/live` — always 200, process-alive only
- `GET /api/health/ready` — 200 if required deps pass, 503 if unhealthy
- `GET /api/health` — full diagnostic JSON, always 200
- Per-check timeout: 3 seconds (`asyncio.wait_for`)
- Status aggregation: healthy → degraded → unhealthy based on required vs. optional classification
- 5 concrete checks: HydraDB (POST to `/recall/full_recall`), LLM (1-token completion), Slack (`auth_test`), Scheduler (`is_running`), DB
- Structured JSON logging of check results

**Strengths:** Endpoint is correctly public (no auth required).  
**Weaknesses:** Hardcoded response, zero dependency visibility.  
**Missing:** Liveness probe, readiness probe, dependency checks, timeout protection, status aggregation.

---

### 5. Retry / Backoff

**Status: FRAMEWORK NOT MERGED — Active infinite-loop bug in production**  
**Score: 2 / 10**

#### A. Implementation Exists?
No generic retry framework. The `backend/retry.py` module does not exist in `main`. Three callers have ad-hoc handling; one has a critical bug.

#### B. Correctly Integrated?
N/A for the framework. The ad-hoc Slack handling is incorrectly implemented (infinite loop, blocking sleep).

#### C. Actually Used?
- HydraDB: zero retries
- LLM: zero retries  
- Slack: infinite retry loop on rate-limiting (no cap, blocking `time.sleep()`)

#### D. Production-Ready?
No. A single transient network error fails a user query with no retry. A sustained Slack rate-limit hangs the ingestion process indefinitely.

#### E. Gaps / Issues

**`hydradb_client.py` — `upload_knowledge()` (lines 182–193):**
```python
try:
    response = requests.post(url, ..., timeout=120)
except requests.RequestException as e:
    print(f"[hydradb] Network error talking to HydraDB: {e}")
    return {}          # ← SILENT FAILURE: caller cannot detect this
```
- Zero retries. Returns empty dict on failure. The ingestion pipeline does not check the return value for errors, so failed uploads are silently dropped.
- 120-second timeout is excessive.

**`hydradb_client.py` — `full_recall()` (lines 268–277):**
```python
try:
    response = requests.post(url, headers=headers, json=payload, timeout=60)
except requests.Timeout as e:
    raise UpstreamTimeoutError(...)
except requests.RequestException as e:
    raise HydraDBError(...)
```
- Zero retries. One network glitch → 500 error returned to the user.

**`llm.py` — `generate_grounded_answer()` (lines 72–90):**
```python
try:
    response = client.chat.completions.create(...)
except APITimeoutError as e:
    raise UpstreamTimeoutError(...)
except Exception as e:
    raise LLMError(...)
```
- Zero retries. Single OpenAI timeout → immediate failure.

**`ingestion/slack_client.py` — `fetch_channel_messages()` (lines 58–84):**
```python
while True:                        # ← NO COUNTER
    try:
        response = self.client.conversations_history(...)
        break
    except SlackApiError as e:
        if self._is_rate_limited(e):
            self._sleep_for_retry(e)
            continue               # ← RETRY FOREVER
        break
```

**`ingestion/slack_client.py` — `_sleep_for_retry()` (lines 220–228):**
```python
def _sleep_for_retry(e: SlackApiError) -> None:
    retry_after = 1
    try:
        retry_after = int(e.response.headers.get("Retry-After", 1))
    except Exception:
        pass
    print(f"[slack_client] Rate limited. Sleeping {retry_after}s and retrying.")
    time.sleep(retry_after)        # ← BLOCKING SLEEP; no backoff; no jitter
```

If Slack continuously rate-limits a channel fetch, the ingestion process will block indefinitely in `time.sleep()` and never complete. The same pattern exists in `fetch_thread_replies()`.

**Retry coverage matrix:**

| Client | Transient error retried? | Auth error excluded? | Backoff? | Jitter? | Async-safe? |
|--------|--------------------------|----------------------|----------|---------|-------------|
| HydraDB `full_recall` | ✗ No | N/A | N/A | N/A | N/A |
| HydraDB `upload_knowledge` | ✗ No | N/A | N/A | N/A | N/A |
| LLM | ✗ No | N/A | N/A | N/A | N/A |
| Slack (rate limit only) | ✓ Yes, infinite | N/A | ✗ No | ✗ No | ✗ Blocking |

**What was designed (not merged, `aayush_health` branch, `backend/retry.py`, 287 lines):**
```python
@retry(service="hydradb", max_attempts=3, initial_delay=0.5)
def full_recall(self, ...): ...

@retry(service="llm", max_attempts=3)
async def generate_grounded_answer(...): ...
```
- Supports both sync and async (uses `asyncio.sleep()` in async paths)
- Exponential backoff: `min(initial_delay × multiplier^(attempt-1), max_delay)`
- Jitter: ±25%
- Retryable HTTP codes: `{429, 500, 502, 503, 504}`
- Non-retryable: `{400, 401, 403, 404, 422}` — auth errors explicitly excluded
- Structured logging: `retry_attempt`, `retry_success`, `retry_exhausted` events
- `NonRetryableError` sentinel to short-circuit remaining attempts

**Strengths:** Error types are well-defined in `errors.py`. Framework design is sound.  
**Weaknesses:** Framework never merged; Slack has an active infinite-loop bug.  
**Missing:** `retry.py`, retry decorators on HydraDB/LLM/Slack, async-safe sleep, backoff, jitter, retry counters.

---

## Bugs Found

---

### Bug 1 — Slack Infinite Retry Loop

**Severity: CRITICAL**  
**Files:** [`backend/ingestion/slack_client.py:58`](backend/ingestion/slack_client.py)

**Problem:** `fetch_channel_messages()` and `fetch_thread_replies()` loop with `while True:` and no counter. If Slack persistently rate-limits a request (e.g., due to expired bot scope or bot being removed from channel), the ingestion process blocks indefinitely in `time.sleep()`.

**Reproduction:**
1. Set `SLACK_BOT_TOKEN` to a token that is rate-limited on the target channel
2. Trigger ingestion
3. Process never terminates; CPU idles; no error is surfaced

**Recommended fix:**
```python
MAX_RETRIES = 5
retries = 0
while retries < MAX_RETRIES:
    try:
        response = self.client.conversations_history(...)
        break
    except SlackApiError as e:
        if self._is_rate_limited(e) and retries < MAX_RETRIES - 1:
            retries += 1
            self._sleep_for_retry(e)
            continue
        raise
```

---

### Bug 2 — Silent Upload Failure

**Severity: CRITICAL**  
**Files:** [`backend/hydradb_client.py:190-192`](backend/hydradb_client.py)

**Problem:** `upload_knowledge()` catches `requests.RequestException` and returns `{}`. The ingestion pipeline does not check this return value, so failed uploads are silently dropped — data is lost with no error surfaced to the user or operator.

**Reproduction:**
1. Make HydraDB unreachable (firewall, wrong URL)
2. Run ingestion
3. No error log beyond one `print()` line; ingestion reports "success"; HydraDB has no data

**Recommended fix:** Raise an exception on network failure instead of returning empty dict. Let the ingestion pipeline decide whether to abort or continue.

---

### Bug 3 — Health Endpoint Always Reports "ok"

**Severity: HIGH**  
**Files:** [`backend/main.py:137-140`](backend/main.py)

**Problem:** `GET /api/health` hardcodes `{"status": "ok"}`. A Kubernetes liveness probe using this endpoint will never report an unhealthy pod, preventing auto-restart on service degradation.

**Reproduction:**
1. Shut down HydraDB
2. `curl http://localhost:8000/api/health`
3. Returns `{"status": "ok"}` — no indication of outage

**Recommended fix:** Implement real dependency checks with timeouts, or at minimum, return a `503` when a critical dependency is unreachable.

---

### Bug 4 — Missing Liveness and Readiness Probes

**Severity: HIGH**  
**Files:** [`backend/main.py`](backend/main.py)

**Problem:** No `/api/health/live` or `/api/health/ready` endpoints. Kubernetes and most PaaS platforms distinguish between liveness (is the process alive?) and readiness (can it serve traffic?). Absence means the platform cannot restart a stuck process or drain traffic during startup.

**Recommended fix:** Add `/api/health/live` returning `{"status": "alive"}` unconditionally, and `/api/health/ready` that checks critical dependencies.

---

### Bug 5 — Blocking `time.sleep()` in Ingestion

**Severity: HIGH**  
**Files:** [`backend/ingestion/slack_client.py:228`](backend/ingestion/slack_client.py)

**Problem:** `_sleep_for_retry()` calls `time.sleep(retry_after)`. In an async FastAPI process, this blocks the entire event loop. Even in a sync ingestion subprocess, it blocks the process from doing anything else during the wait.

**Recommended fix:** Replace with `asyncio.sleep()` in async contexts; or use a proper retry framework that manages sleep externally.

---

### Bug 6 — HydraDB Response Body Logged Unredacted

**Severity: HIGH**  
**Files:** [`backend/hydradb_client.py:196`](backend/hydradb_client.py), [`backend/hydradb_client.py:293`](backend/hydradb_client.py)

**Problem:**
- Line 196: `print(f"[hydradb] Response body: {response.text}")` — full body on every response
- Line 293: `log_context=f"full_recall HTTP {response.status_code} body={response.text[:400]}"` — first 400 chars of body in error path

If HydraDB embeds sensitive information in error responses (internal user data, system paths, API internals), it reaches stdout unredacted.

**Recommended fix:** Log only status code and a safe subset of error fields. Never log raw response bodies.

---

### Bug 7 — CI Quality Gates Partially Disabled

**Severity: MEDIUM**  
**Files:** `.github/workflows/quality.yml` (deleted; historical)

**Problem:** In the deleted `quality.yml`, both `black` and `isort` steps had `continue-on-error: true`. Only `flake8` could actually block a merge. Formatting violations were reported but never enforced.

**Recommended fix:** Remove `continue-on-error: true` from all quality gates, or explicitly document that they are advisory-only.

---

### Bug 8 — Smoke Validation Imports Phantom Modules

**Severity: MEDIUM**  
**Files:** `.github/workflows/ci.yml` (deleted; historical)

**Problem:** The smoke-validation CI job imported `logging_config`, `request_context`, and `realtime_ingest` — none of which exist in the current `main` branch. This job would fail immediately on every run against HEAD.

**Recommended fix:** Sync smoke imports with actual module inventory. Add a pre-merge check that CI module list matches actual `backend/` contents.

---

### Bug 9 — No Request Correlation IDs

**Severity: MEDIUM**  
**Files:** [`backend/main.py`](backend/main.py), all backend modules

**Problem:** No request ID is generated or propagated. A log line in `hydradb_client.py` cannot be linked to the originating HTTP request in `main.py`. Debugging multi-step query failures requires manual timestamp correlation.

**Recommended fix:** Add UUID-generating middleware; thread `request_id` through via Python `contextvars`; include in every log line.

---

### Bug 10 — `traceback.print_exc()` in Scheduler

**Severity: LOW**  
**Files:** [`backend/scheduler.py:71`](backend/scheduler.py)

**Problem:** `traceback.print_exc()` emits a multi-line stack trace to stdout. Log aggregators treat each line as a separate event, breaking the trace across many log records and making root-cause analysis very difficult.

**Recommended fix:** Use `logger.exception("Ingestion job failed")` which serializes the full traceback as a single structured log record.

---

## Architectural Concerns

**1. Feature branch never merged**  
`aayush_health` branch contains ~1,000 lines of well-designed, tested code (`health.py`, `retry.py`, `logging_config.py`) that directly addresses three of the five audit items. The branch was abandoned rather than merged. This represents significant wasted effort and means the production branch lacks infrastructure the team clearly intended to ship.

**2. Silent failures in the ingestion pipeline**  
The ingestion pipeline treats `{}` from `upload_knowledge()` as success, does not validate batch results, and has no failed-upload tracking. Data can be permanently lost without any alert.

**3. No correlation between ingestion runs and their outcomes**  
The scheduler fires `run_ingestion()` in a subprocess. There is no job ID, no outcome persistence, and no way to query "did the last run succeed and how many messages were ingested?" from the API.

**4. Single-attempt calls to all external services**  
HydraDB, OpenAI LLM, and Slack (for non-rate-limit errors) have single-attempt calls with no retry. In a cloud environment with ~0.1% transient error rates, a query pipeline making 3 external calls will fail roughly 0.3% of the time due to transient issues alone — preventable with retry logic.

**5. `print()` as the sole logging mechanism**  
The entire observability story is "read stdout." There is no way to set log levels, filter by component, search by request ID, or ingest logs into any aggregation system without a custom parser.

---

## Technical Debt

| Item | Effort | Risk if Ignored |
|------|--------|-----------------|
| Restore and re-integrate 28 test files | Medium | Regressions go undetected |
| Restore and fix 2 CI workflow files | Small | Every push is unvalidated |
| Replace `print()` with structured logging | Medium | Undebuggable in production |
| Merge `health.py` from `aayush_health` branch | Small | Monitoring/orchestration blind |
| Merge `retry.py` from `aayush_health` branch | Small | Transient failures cause user-facing errors |
| Fix Slack infinite loop | Small | Ingestion can hang indefinitely |
| Fix silent `upload_knowledge()` failure | Small | Data loss with no alert |
| Add response body redaction to HydraDB logs | Trivial | Potential sensitive data in logs |

---

## Missing Hardening

- No rate-limit enforcement on the streaming endpoint (`/api/query/stream`)
- No circuit breaker — cascading failure across HydraDB + LLM will exhaust all connections
- No request body size limit beyond FastAPI defaults (Slack event payloads could be large)
- No secrets scanning in CI (no `gitleaks` or similar)
- No dependency vulnerability scanning (`pip audit` / `safety`)
- No SBOM generation
- `DEBUG_RECALL=true` in `.env` would log full Slack message content — no warning in `.env.example` (file doesn't exist)
- No timeout on the root `GET /` endpoint or other non-health endpoints

---

## Recommended Next Steps

**Priority 1 — Stop the bleeding (this week):**

1. Fix the Slack infinite retry loop (`slack_client.py:58`) — add a counter and maximum retry cap
2. Fix silent upload failure (`hydradb_client.py:192`) — raise on error instead of returning `{}`
3. Restore `backend/tests/` from git history (pre-`609741a` commit) and fix the `realtime_ingest` import error in `test_api.py:370`
4. Restore `.github/workflows/` from git history and fix the phantom module imports in smoke validation

**Priority 2 — Merge the feature branch work (next sprint):**

5. Merge `health.py` from `aayush_health` into `main` — provides liveness/readiness probes and real dependency checks
6. Merge `retry.py` from `aayush_health` into `main` — apply `@retry` decorator to `hydradb_client.full_recall()`, `llm.generate_grounded_answer()`, and replace the Slack manual loop
7. Merge `logging_config.py` and `request_context.py` from `aayush_health` — replace all `print()` calls with structured logger

**Priority 3 — Harden (next month):**

8. Add `backend/.env.example` with documentation for all required env vars
9. Remove `continue-on-error: true` from black/isort in `quality.yml`
10. Add `pip audit` and `npm audit` steps to CI
11. Add response body redaction to `hydradb_client.py`
12. Add request-ID middleware and thread through all log calls
13. Implement circuit breaker for HydraDB and LLM calls
14. Document `DEBUG_RECALL=true` security implications in README

---

*This audit is based on static code inspection of HEAD (`609741a`) and git history review. No tests were run (none exist). All line numbers reference the current main branch.*
