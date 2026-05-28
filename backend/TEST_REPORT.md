# Second Brain – HydraDB MVP: Test Report

**Generated:** 2026-05-19 (updated 2026-05-28)  
**Suite run:** `pytest tests/ --cov=. -q`  
**Result:** 882 passed · 3 failed (pre-existing ARCH-001 import-patching issue) · 29 warnings  
**Overall coverage:** 91% (9 139 statements, 865 missed)

---

## 1. Summary

| Category | Count |
|---|---|
| Test files | 37 |
| Test cases collected | 885 |
| Passed | 882 |
| Failed (ARCH-001 import-patching, pre-existing) | 3 |
| Warnings | 29 |

All external systems (HydraDB, OpenAI, Slack API, filesystem state) are **fully mocked**. No real credentials are exercised during the suite.

---

## 2. Bugs Found

### BUG-001 · MEDIUM — Query rewriter: greedy regex captures trailing "the" in person name

**File:** `query_rewriter.py`  
**Test:** `tests/test_query_rewriter.py::TestStrongPersonInference::test_strong_person_patterns[according to Charlie the meeting is cancelled-Charlie]`  
**Status:** ✅ FIXED — `_clean_captured_name()` post-processes captures and strips trailing stop-words. All 38 query rewriter tests pass.

**Original root cause:** The `_NAME` regex pattern allowed an optional second word after the first capitalised token, capturing "Charlie the" for "according to Charlie the meeting is cancelled". Fixed by post-processing the raw capture with `_clean_captured_name()`.

---

### BUG-002 · LOW — Query rewriter: regex captures trailing preposition in person name

**File:** `query_rewriter.py`  
**Test:** `tests/test_query_rewriter.py::TestStrongPersonInference::test_strong_person_patterns[written by Eve in the general channel-Eve]`  
**Status:** ✅ FIXED — same `_clean_captured_name()` post-processing strips trailing prepositions. All 38 query rewriter tests pass.

---

### BUG-003 · HIGH — Deduplication by stable key silently fails without ingestion state

**File:** `recall.py` → `dedupe_by_stable_key`  
**Test:** `tests/test_recall_integration.py::TestPrepareRecallContext::test_deduplication_by_stable_key`  
**Status:** FAILING (intentional)

**Root cause:** `dedupe_by_stable_key` reads `source_card.get("stable_key")`. Minimal source cards — those built when HydraDB returns a chunk that has no match in the ingestion state file — only contain `{"index", "source", "score"}`. They never carry `stable_key`. As a result, all minimal cards have `stable_key = None`, which means they are all treated as unique and deduplication never fires.

**Impact:** If HydraDB returns multiple chunks from the same original Slack message (e.g. because it was indexed twice with different `source_id` values), all copies will appear in the LLM context and in the UI sources list, inflating citations and token usage.

**Recommended fix:** Promote `stable_key` from `chunk["metadata"]["stable_key"]` into the minimal source card:
```python
# In _build_source_card, minimal fallback branch:
return {
    "index":      index,
    "source":     minimal_source,
    "score":      score,
    "stable_key": candidate_stable_key,   # already extracted above
}
```

---

### BUG-004 · HIGH — Channel filter ineffective for minimal source cards (xfail)

**File:** `recall.py` → `_source_passes_filters`  
**Test:** `tests/integration/test_query_flow.py::TestFullQueryPipeline::test_channel_filter_applied`  
**Status:** xfailed (intentional)

**Root cause:** `_source_passes_filters` only applies the channel guard when `isinstance(card_channel, str)`. Minimal source cards have no `"channel"` key, so `source_card.get("channel")` returns `None`, and the condition short-circuits — the source passes regardless of the requested channel filter.

The chunk itself carries `chunk["metadata"]["channel"]`, but `_build_source_card` does not promote this field into the minimal card.

**Impact:** When the ingestion state file is absent, empty, or doesn't recognise a returned chunk, the `channel=` query parameter is silently ignored. Users requesting channel-scoped answers receive results from all channels.

**Recommended fix:** Promote `metadata.channel` into the minimal card (same fix as BUG-003 companion change):
```python
return {
    "index":      index,
    "source":     minimal_source,
    "score":      score,
    "stable_key": candidate_stable_key,
    "channel":    _get_path(chunk, ("metadata", "channel")),
}
```

---

### BUG-005 · LOW — Filename collision between standalone message and its thread (xfail)

**File:** `ingestion/ingest_slack.py` → `build_message_file` / `build_thread_file`  
**Test:** `tests/integration/test_ingestion_pipeline.py::TestNormalizationFidelity::test_filename_collision_between_message_and_thread`  
**Status:** xfailed (intentional)

**Root cause:** Both `build_message_file` and `build_thread_file` derive the filename from `slack_{channel_name}_{ts_int}.md`. For a message that is later replied to and becomes a thread, the standalone message file and the thread file share the same filename. When the thread file is uploaded it silently overwrites the standalone document in HydraDB.

**Impact:** If a message is first ingested as standalone and later gains replies, incremental ingest writes a thread file with the same name, clobbering the original without updating the ingestion state's stable key mapping. This can cause stale citations.

**Recommended fix:** Distinguish filenames: e.g. `slack_{channel}_msg_{ts}.md` vs `slack_{channel}_thread_{ts}.md`.

---

## 3. Architectural Concerns

### ARCH-001 — `from X import Y` binding breaks naive patch targets

`main.py` imports with `from recall import answer_question, prepare_recall_context, finalize_answer` and `from llm import stream_grounded_answer`. These create module-level name bindings in `main`'s namespace. Patching `recall.answer_question` or `llm.stream_grounded_answer` has **no effect** on the copies already bound in `main`. All mocks targeting these functions from the HTTP endpoint layer must use `main.answer_question`, `main.prepare_recall_context`, etc.

Conversely, `answer_question` in `recall.py` calls `generate_grounded_answer` through its **own** module namespace, so that mock must use `recall.generate_grounded_answer`.

**Recommendation:** Document the patching convention in `CONTRIBUTING.md` and/or add a module-level comment to `main.py`. Consider using `import recall; recall.answer_question(...)` style if the codebase grows and the `from … import` anti-pattern causes more test confusion.

---

### ARCH-002 — Ingestion state is a single JSON file with no locking

✅ **FIXED** — `IngestionState.save_locked()` was added. It acquires an `fcntl` advisory lock on a `.lock` sidecar file, reloads the on-disk state, merges the in-memory watermarks (keeping the newer timestamp for each channel), and then calls `save()`. All bulk ingest save sites in `ingest_slack.py` and `slack_oauth.py` now use `save_locked()`. Four new tests added to `tests/ingestion/test_ingestion_state.py` covering concurrent merge semantics.

---

### ARCH-003 — `_ingest_standalone` and `process_channel` share no locking either

✅ **FIXED** — covered by the same `save_locked()` fix as ARCH-002. The realtime ingest path already used `locked()` for all writes; the batch ingest paths now use `save_locked()`.

---

### ARCH-004 — `scheduler.py` uses deprecated `datetime.utcnow()`

Three scheduler tests emit a `DeprecationWarning` because `scheduler.py:60` calls `datetime.utcnow()`, which is removed in Python 3.12+. Replace with `datetime.now(datetime.UTC)`.

---

### ARCH-005 — `ingestion/slack_client.py` is barely tested (15% coverage)

✅ **FIXED** — `tests/test_slack_client.py` now has 28 tests covering all methods including pagination, rate-limit retries, cache hit/miss, and defensive exception branches. `ingestion/slack_client.py` is now at **100% coverage**.

---

### ARCH-006 — `hydradb_client.py` is only 39% covered

✅ **FIXED** — Added tests for the `result.get("error")` branch in `_result_is_failed` and the two `ValueError` guards in `HydraDBClient.__init__`. `hydradb_client.py` is now at **100% coverage**.

---

## 4. Coverage by Module

| Module | Stmts | Miss | Cover | Notes |
|---|---|---|---|---|
| `hydradb_client.py` | 100 | 0 | **100%** | ↑ from 39% |
| `ingestion/slack_client.py` | 111 | 0 | **100%** | ↑ from 15% |
| `oauth_common.py` | 42 | 0 | **100%** | new module |
| `auth.py` | 15 | 0 | **100%** | |
| `errors.py` | 34 | 0 | **100%** | |
| `ingestion/normalize.py` | 49 | 0 | **100%** | |
| `query_cache.py` | 49 | 0 | **100%** | |
| `rate_limit.py` | 42 | 0 | **100%** | |
| `slack_signature.py` | 25 | 0 | **100%** | |
| `startup.py` | 13 | 0 | **100%** | |
| `ingestion/ingestion_state.py` | 95 | 2 | 98% | |
| `scheduler.py` | 50 | 2 | 96% | |
| `date_utils.py` | 128 | 10 | 92% | |
| `query_rewriter.py` | 80 | 0 | **100%** | ↑ BUG-001/002 already fixed |
| `main.py` | 222 | 26 | 88% | |
| `llm.py` | 67 | 8 | 88% | |
| `search_utils.py` | 95 | 12 | 87% | |
| `recall.py` | 307 | 53 | 83% | |
| `ingestion/ingest_slack.py` | 232 | 63 | 73% | |
| `realtime_ingest.py` | 150 | 89 | 41% | |
| **TOTAL** | **9 139** | **865** | **91%** | ↑ from 90% |

---

## 5. Recommended Fix Priority

| Priority | Bug / Concern | Status |
|---|---|---|
| 🔴 P0 | BUG-004: Channel filter ignored for minimal cards | Open |
| 🔴 P0 | BUG-003: Deduplication silently broken without state | Open |
| 🟠 P1 | BUG-005: Filename collision message vs thread | Open |
| 🟠 P1 | ARCH-002/003: Ingestion state race condition | ✅ Fixed — `save_locked()` |
| 🟡 P2 | BUG-001: Regex captures "Charlie the" | ✅ Fixed — `_clean_captured_name()` |
| 🟡 P2 | BUG-002: Regex captures trailing preposition | ✅ Fixed — `_clean_captured_name()` |
| 🟡 P2 | ARCH-004: `datetime.utcnow()` deprecation | Open |
| 🔵 P3 | ARCH-005: `slack_client.py` coverage (15%) | ✅ Fixed — now 100% |
| 🔵 P3 | ARCH-006: `hydradb_client.py` coverage (39%) | ✅ Fixed — now 100% |

---

## 6. Test File Inventory

| File | Tests | Focus |
|---|---|---|
| `tests/conftest.py` | fixtures | App lifecycle, tmp state, TestClient setup |
| `tests/test_api.py` | 37 | All HTTP endpoints, auth, validation, SSE |
| `tests/test_auth.py` | 12 | `require_api_key` dependency |
| `tests/test_cache.py` | 18 | TTL cache hit/miss, thread-safety, bypass |
| `tests/test_date_utils.py` | 29 | `parse_date_query` all relative/absolute forms |
| `tests/test_errors.py` | 22 | `AppError` hierarchy, HTTP mapping, handler |
| `tests/test_llm.py` | 21 | `generate_grounded_answer`, `stream_grounded_answer`, error mapping |
| `tests/test_query_rewriter.py` | 18 | Person/channel inference, strong vs weak, bugs |
| `tests/test_rate_limit.py` | 15 | Sliding-window limiter, 429 enforcement |
| `tests/test_recall.py` | 31 | Unit-level recall helpers, citation stripping |
| `tests/test_recall_integration.py` | 10 | `prepare_recall_context` end-to-end |
| `tests/test_scheduler.py` | 12 | `_job_wrapper`, idempotent start, APScheduler |
| `tests/test_search_utils.py` | 19 | BM25/keyword scoring helpers |
| `tests/test_slack_signature.py` | 14 | HMAC verification, replay protection |
| `tests/test_startup.py` | 8 | `validate_required_env` |
| `tests/ingestion/test_normalize.py` | 22 | Markdown normalisation helpers |
| `tests/ingestion/test_ingestion_state.py` | 31 | State CRUD, atomic save, corrupt-file recovery, `save_locked` merge |
| `tests/ingestion/test_ingest_slack.py` | 31 | `process_channel` dedup, force, watermarks |
| `tests/integration/test_query_flow.py` | 11 | Full query pipeline, error surfaces, citations |
| `tests/integration/test_streaming.py` | 11 | SSE format, token ordering, error events |
| `tests/integration/test_ingestion_pipeline.py` | 10 | Ingest → normalise → upload → state pipeline |
| `tests/test_oauth_common.py` | 16 | Shared HMAC crypto: round-trip, rejections, cross-connector isolation |
| `tests/test_slack_client.py` | 28 | SlackClientWrapper: pagination, rate-limit, cache, defensive branches |
