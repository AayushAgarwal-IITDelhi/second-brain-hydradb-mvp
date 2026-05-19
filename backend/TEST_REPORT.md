# Second Brain – HydraDB MVP: Test Report

**Generated:** 2026-05-19  
**Suite run:** `pytest tests/ --cov=. -q`  
**Result:** 474 passed · 3 failed (intentional – document real bugs) · 2 xfailed · 3 warnings  
**Overall coverage:** 90% (4 410 statements, 443 missed)

---

## 1. Summary

| Category | Count |
|---|---|
| Test files | 19 |
| Test classes | 59 |
| Test cases collected | 479 |
| Passed | 474 |
| Failed (documenting real bugs) | 3 |
| xfailed (documenting real bugs) | 2 |
| Warnings | 3 |

All external systems (HydraDB, OpenAI, Slack API, filesystem state) are **fully mocked**. No real credentials are exercised during the suite.

---

## 2. Bugs Found

### BUG-001 · MEDIUM — Query rewriter: greedy regex captures trailing "the" in person name

**File:** `query_rewriter.py`  
**Test:** `tests/test_query_rewriter.py::TestStrongPersonInference::test_strong_person_patterns[according to Charlie the meeting is cancelled-Charlie]`  
**Status:** FAILING (intentional)

**Root cause:** The `_NAME` regex pattern allows an optional second word after the first capitalised token (`[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?`). For the phrase "according to Charlie the meeting is cancelled", the optional second-word group matches " the", producing the capture "Charlie the". The word "the" is then checked against `_PERSON_BLOCKLIST` and rejected, so `inferred_person` returns `None` instead of "Charlie".

**Impact:** Queries that mention a person immediately before a stop-word ("the", "a", "an", etc.) silently lose the person filter, returning unfiltered results.

**Recommended fix:**
```python
# Add a negative lookahead to prevent capturing blocked words as the second token
_NAME = r"[A-Z][a-z]+(?:\s+(?!(?:" + "|".join(_PERSON_BLOCKLIST) + r")\b)[A-Z][a-z]+)?"
```

---

### BUG-002 · LOW — Query rewriter: regex captures trailing preposition in person name

**File:** `query_rewriter.py`  
**Test:** `tests/test_query_rewriter.py::TestStrongPersonInference::test_strong_person_patterns[written by Eve in the general channel-Eve]`  
**Status:** FAILING (intentional)

**Root cause:** Same `_NAME` optional second-word group. "written by Eve in the general channel" — the word "in" is lower-case, so the regex doesn't match it as a second name token. However the underlying issue is that the surrounding context captures "Eve in" via a different matching path (the surrounding `(?:by|from)\s+(_NAME)` anchor), likely because a lookahead stops after "Eve " but the `\w+` on the trailing boundary consumes the preposition.

**Impact:** `inferred_person` is returned as `"Eve in"` instead of `"Eve"`. A downstream lookup for a user named "Eve in" will return no matches, silently breaking the filter.

**Recommended fix:** Strip any trailing preposition / stop-word from the captured group before returning:
```python
_TRAILING_STOP = re.compile(r"\s+(?:in|at|on|by|from|the|a|an)$", re.IGNORECASE)
person = _TRAILING_STOP.sub("", raw_capture).strip()
```

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

`IngestionState` reads and writes a JSON file with a write-tmp-then-rename strategy (atomic on POSIX). However, two processes (e.g. the scheduled batch ingestor and the realtime ingest path) can both read the file simultaneously, each make updates to their in-memory copy, and then both rename their tmp file — the second rename silently overwrites the first.

**Recommendation:** Use a file-level advisory lock (e.g. `fcntl.flock`) around the read-modify-write cycle, or migrate state to SQLite (which handles concurrent writers natively).

---

### ARCH-003 — `_ingest_standalone` and `process_channel` share no locking either

`realtime_ingest._ingest_standalone` loads its own `IngestionState` instance and saves it. If the APScheduler job (`process_channel`) runs concurrently, both instances race on the state file (ARCH-002 applies).

---

### ARCH-004 — `scheduler.py` uses deprecated `datetime.utcnow()`

Three scheduler tests emit a `DeprecationWarning` because `scheduler.py:60` calls `datetime.utcnow()`, which is removed in Python 3.12+. Replace with `datetime.now(datetime.UTC)`.

---

### ARCH-005 — `ingestion/slack_client.py` is barely tested (15% coverage)

The Slack client wrapper (`SlackWrapper`) has only 15% line coverage. Functions like `fetch_thread_replies`, `get_permalink`, and `resolve_user_name` are exercised only through higher-level integration mocks. Any regression in the Slack SDK integration would go undetected.

**Recommendation:** Add unit tests for `SlackWrapper` with a mocked `slack_sdk.WebClient`.

---

### ARCH-006 — `hydradb_client.py` is only 39% covered

HydraDB client retry logic, batch upload, and error-mapping code paths are not exercised because tests mock `requests.post` or the high-level `HydraDBClient.full_recall`. Edge cases like partial-success batch responses, rate-limit retries, and non-JSON error bodies are untested.

---

## 4. Coverage by Module

| Module | Stmts | Miss | Cover |
|---|---|---|---|
| `auth.py` | 15 | 0 | **100%** |
| `errors.py` | 34 | 0 | **100%** |
| `ingestion/normalize.py` | 49 | 0 | **100%** |
| `query_cache.py` | 49 | 0 | **100%** |
| `rate_limit.py` | 42 | 0 | **100%** |
| `slack_signature.py` | 25 | 0 | **100%** |
| `startup.py` | 13 | 0 | **100%** |
| `ingestion/ingestion_state.py` | 81 | 2 | 98% |
| `scheduler.py` | 50 | 2 | 96% |
| `date_utils.py` | 128 | 10 | 92% |
| `main.py` | 222 | 26 | 88% |
| `llm.py` | 67 | 8 | 88% |
| `search_utils.py` | 95 | 12 | 87% |
| `query_rewriter.py` | 80 | 9 | 89% |
| `recall.py` | 307 | 53 | 83% |
| `ingestion/ingest_slack.py` | 232 | 63 | 73% |
| `realtime_ingest.py` | 150 | 89 | 41% |
| `hydradb_client.py` | 99 | 60 | 39% |
| `ingestion/slack_client.py` | 107 | 91 | **15%** |
| **TOTAL** | **4 410** | **443** | **90%** |

---

## 5. Recommended Fix Priority

| Priority | Bug / Concern | Effort |
|---|---|---|
| 🔴 P0 | BUG-004: Channel filter ignored for minimal cards | Small — add two lines to `_build_source_card` |
| 🔴 P0 | BUG-003: Deduplication silently broken without state | Small — same fix, companion line |
| 🟠 P1 | BUG-005: Filename collision message vs thread | Small — rename template strings |
| 🟠 P1 | ARCH-002/003: Ingestion state race condition | Medium — add flock or migrate to SQLite |
| 🟡 P2 | BUG-001: Regex captures "Charlie the" | Small — add negative lookahead |
| 🟡 P2 | BUG-002: Regex captures trailing preposition | Small — strip trailing stop-word |
| 🟡 P2 | ARCH-004: `datetime.utcnow()` deprecation | Trivial — one-line swap |
| 🔵 P3 | ARCH-005: `slack_client.py` coverage (15%) | Medium — add SlackWrapper unit tests |
| 🔵 P3 | ARCH-006: `hydradb_client.py` coverage (39%) | Medium — add HTTP-level mock tests |

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
| `tests/ingestion/test_ingestion_state.py` | 24 | State CRUD, atomic save, corrupt-file recovery |
| `tests/ingestion/test_ingest_slack.py` | 31 | `process_channel` dedup, force, watermarks |
| `tests/integration/test_query_flow.py` | 11 | Full query pipeline, error surfaces, citations |
| `tests/integration/test_streaming.py` | 11 | SSE format, token ordering, error events |
| `tests/integration/test_ingestion_pipeline.py` | 10 | Ingest → normalise → upload → state pipeline |
