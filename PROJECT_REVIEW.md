# Project Review — Second Brain HydraDB MVP

Review date: 2026-05-28
Branch reviewed: `slack_cleanup`

---

## Executive Summary

**Overall score: 7.5 / 10**

**Production readiness: Mostly Ready**

The codebase is well-architected and demonstrates uncommon discipline for
an MVP: startup validation, structured JSON logging, HMAC-signed OAuth
state, per-route rate limiting, typed error hierarchy, dead-letter logging,
Sentry integration, and 90% backend test coverage. Multi-tenancy is
properly enforced at both the ingestion and retrieval layers.

The main risk areas before launch are documentation (README was Phase 1–2
while the code is Phase 8), a small number of high-priority bugs documented
in `TEST_REPORT.md`, and the ingestion state file having no file-level
locking for concurrent access. Neither blocks launch on its own, but both
should be addressed in the first post-launch sprint.

---

## README Findings

The README was written at Phase 1–2 and had not been updated to reflect
eight phases of development. Key mismatches (now fixed in this review):

| Area | Was (stale) | Is (fixed) |
|---|---|---|
| Auth description | "FastAPI with X-API-Key auth for all `/api/*` routes" | Supabase JWT for all user routes; X-API-Key only on legacy `/api/admin/status` |
| API endpoint table | 6 routes | 28 routes |
| Env var table | 17 vars, mostly legacy CLI | 40+ vars split into Required / Slack OAuth / Gmail OAuth / Optional |
| Default values | `QUERY_CACHE_MAX_SIZE=256`, `AUTO_INGEST_INTERVAL_MINUTES=30`, `RATE_LIMIT_PER_5_MIN=30` | `100`, `15`, `20` (confirmed against code) |
| Folder structure | 15 files, missing Phase 3–8 modules | All 28 backend modules + frontend auth/, slack/, gmail/ subdirs |
| Tech stack | Slack + OpenAI only | + Gmail, Supabase, PyJWT, google-auth-oauthlib |
| Setup instructions | "copy .env, set SLACK_BOT_TOKEN" | Supabase project creation + SQL migrations + Slack OAuth app + optional Gmail app |
| Frontend description | Single-user, no auth | Multi-user Supabase auth, workspace switcher, connector settings panels |
| Ingestion description | CLI-first | Per-workspace OAuth connect via UI (CLI is legacy path) |
| Notes section | "frontend stores everything in localStorage" | Chat + saved answers persisted in Supabase |

---

## Cleanup Findings

### Code cleanup

**Fixed in this review:**

| Issue | File | Fix applied |
|---|---|---|
| `_redirect_with_status` and `_gmail_redirect_with_status` were identical functions with different names | `backend/main.py:1256,1512` | Unified into `_oauth_redirect(frontend, connector, result, reason)`; both old names kept as thin shims |
| Stale comment referencing internal bug-tracking IDs "BUG-003 / BUG-004 fix" | `backend/recall.py:380` | Removed tracking IDs; kept the actual explanation |
| Stale comment "See TODO in the README" pointing to a TODO that no longer exists | `backend/ingestion/normalize.py:92` | Replaced with accurate one-liner |

**Still present (not addressed — see priority table):**

- `backend/slack_oauth.py` and `backend/gmail_oauth.py` both implement
  `make_oauth_state`, `verify_oauth_state`, `_b64url_encode`, `_b64url_decode`
  identically (~100 lines duplicated). If a crypto fix is needed in one, the
  other lags. Refactor to `oauth_common.py` when convenient.

- `backend/App.jsx` (~2,300 lines) mixes query logic, chat history, saved
  answers, export helpers, and UI components. Not a bug, but maintenance
  burden grows with every feature.

- `backend/main.py` (~1,700 lines) contains all routes, request models,
  response models, and auth dependencies.

### Documentation cleanup

**Fixed in this review:**

- README fully rewritten to match Phase 8 implementation (see README
  Findings above).

**Still present:**

- `backend/TEST_REPORT.md` documents 5 bugs and 6 architectural concerns
  under their original identifiers (BUG-001 through BUG-005, ARCH-001
  through ARCH-006). BUG-003 and BUG-004 references are now removed from
  `recall.py` but still accurate in the report itself; no change needed
  there.

### Configuration cleanup

All environment variable names are consistent and follow the pattern
`SERVICE_FEATURE_SUBFEATURE` (e.g. `RATE_LIMIT_AUTH_PER_5_MIN`,
`GMAIL_MAX_MESSAGES_PER_RUN`). The `.env.example` is accurate and
complete.

One minor inconsistency: `HYDRADB_SUB_TENANT_ID` is a legacy CLI-only
variable but is listed alongside required HydraDB vars in the old README.
Fixed in the new README (moved to Optional / legacy section).

### Test cleanup

**Coverage gaps (not blocking launch):**

| Gap | Severity |
|---|---|
| No frontend tests at all (no Jest / React Testing Library) | Medium |
| `slack_client.py` (Slack API wrapper) at 15% coverage | Medium |
| `hydradb_client.py` at 39% coverage | Medium |
| JWKS asymmetric path (ES256/RS256) only tested via unit mocks, no integration | Low |
| Cross-workspace OAuth state tamper (state embeds workspace_id, no test for forged state) | Low |

**Tests that exist and pass:**
- 37 backend unit + integration test files, 479 tests total
- 90% overall coverage
- CI gate at 85%

### Architecture cleanup

**High-severity (address in first post-launch sprint):**

- **ARCH-002 / ARCH-003** — Ingestion state (`data/ingestion_state.json`)
  uses an atomic write-temp-rename pattern but has no cross-process file
  locking. If the APScheduler background job and a manual ingest triggered
  via the API run concurrently, the state file can be corrupted. Mitigation:
  add a `filelock` around all reads/writes, or migrate to a SQLite-backed
  state store.

**Medium-severity (schedule in next sprint):**

- **ARCH-005** — `backend/ingestion/slack_client.py` (the Slack API wrapper
  used by both ingestion paths) has only 15% test coverage. It is the
  lowest-covered module and a common failure point.

- OAuth state tokens are not single-use. In theory, if a browser retries the
  OAuth callback, the same state is accepted again and can overwrite the
  existing installation. Adding an `oauth_state_used` table would close this.

**Low-severity:**

- **ARCH-001** — Some test files use `from X import Y` import patterns that
  break naive `unittest.mock.patch` targets. Affects test isolation, not
  production code.

- `backend/supabase_client.py` is 1,681 lines — a single file for all
  Supabase operations. Functional but will become hard to navigate.

---

## Technical Debt

| Item | Severity | Effort | Notes |
|---|---|---|---|
| Ingestion state file has no cross-process lock (ARCH-002/003) | High | Small (add `filelock`) | Concurrent ingest can corrupt dedup watermarks |
| OAuth state tokens are reusable (no dedupe table) | Medium | Medium | Low practical risk; single-user OAuth flows don't retry |
| `slack_client.py` undertested (15% coverage) | Medium | Medium | Add happy-path + rate-limit tests |
| OAuth state logic duplicated in `slack_oauth.py` and `gmail_oauth.py` | Medium | Medium | Extract to `oauth_common.py` |
| `App.jsx` monolithic (2,300 lines) | Medium | Large | Split into focused components; no urgency until new features are added |
| `main.py` monolithic (1,700 lines) | Medium | Large | Same as above |
| No frontend tests | Medium | Large | React Testing Library + Vitest; cover auth flow + query submission |
| `supabase_client.py` monolithic (1,681 lines) | Low | Large | Functional; split by domain (auth, slack, gmail, chat) |
| BUG-001: Person name regex captures trailing "the" (e.g. "Charlie the") | Low | Small | Fix regex in `query_rewriter.py` |
| BUG-002: Person name regex captures trailing preposition | Low | Small | Same file |
| BUG-004 (xfail): Channel filter ineffective for minimal source cards | Low | Medium | Only affects cards missing metadata |
| BUG-005 (xfail): Filename collision between message and thread keys | Low | Small | Edge case |
| `AuthContext.jsx` swallows session hydration errors silently | Low | Small | Show toast on Supabase auth failure |

---

## Recommended Cleanup Priority

### Critical (block launch or risk data corruption)

_None that block launch. The ingestion state race is the closest, but it
requires a specific concurrent trigger pattern unlikely in single-user local
deployments._

### High (first post-launch sprint)

1. **ARCH-002/003** — Add file-level locking to `ingestion/ingestion_state.py`.
   One-line fix with `filelock` (already in common use). Prevents silent
   dedup corruption under concurrent ingest.

2. **ARCH-005** — Add unit tests for `ingestion/slack_client.py` covering
   pagination, rate-limit back-off, and error paths. Currently 15% covered
   and a common real-world failure point.

### Medium (second sprint)

3. Extract shared OAuth state logic from `slack_oauth.py` and `gmail_oauth.py`
   into `backend/oauth_common.py`. ~100 lines of duplicate crypto code.

4. Add `oauth_state_used` table to deduplicate OAuth callback retries.

5. Fix `query_rewriter.py` regex bugs (BUG-001, BUG-002) — minor but
   produces noisy inferred names for certain query phrasings.

6. Increase `hydradb_client.py` coverage (39% → ≥70%).

### Low (backlog)

7. Add frontend test suite (Jest / Vitest + React Testing Library).

8. Surface Supabase session hydration errors to the user
   (`AuthContext.jsx` currently swallows them).

9. Split `App.jsx`, `main.py`, `supabase_client.py` into focused modules
   when the next round of feature work starts — don't refactor for its own
   sake.

10. Add `oauth_state_used` table for full single-use state enforcement.

---

## Changes made in this review

The following changes were made to the codebase as part of this review:

### README.md — full rewrite

Rewrote to reflect Phase 8 implementation. See README Findings above for the
complete list of corrected content.

### backend/main.py — unified OAuth redirect helper

`_redirect_with_status` (line 1256) and `_gmail_redirect_with_status`
(line 1512) were structurally identical. Both now delegate to a single
`_oauth_redirect(frontend, connector, result, reason)` helper. The old names
are kept as one-line shims so call sites are unchanged.

### backend/recall.py — removed stale comment

Removed the `(BUG-003 / BUG-004 fix)` tag from line 380. The explanation of
what the code does is preserved; the internal tracking reference is not.

### backend/ingestion/normalize.py — removed stale comment

Removed "See TODO in the README" from the permalink comment (line 92). The
README TODO it referenced no longer exists. Replaced with an accurate
description of where permalink population actually happens.
