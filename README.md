# Second Brain

A multi-tenant "second brain" that ingests your Slack channels and Gmail
into HydraDB, retrieves relevant context for any question, and streams a
grounded answer with cited sources via a cloud LLM. Comes with a React +
Vite frontend for streaming Q&A, multi-turn conversations, saved answers,
connector management, and dark mode.

## Features

**Search & retrieval**
- HydraDB semantic recall with local re-ranking
- **Exact mode** — prefer chunks containing literal keyword matches
- **Hybrid mode** — combine semantic + keyword scoring
- **Person/channel-aware query rewriting** — "what did Rahul say in product?"
  automatically infers `user=Rahul`, `channel=product` and applies them as
  hard filters (strong inference) or ranking bias (weak inference)
- **Natural-language date parsing** — `last week`, `yesterday`,
  `after May 10`, `from May 1 to May 7`, etc. resolved server-side
- **Query TTL cache** (bypassed when conversation history is present)
- Citation cleanup (`[N]` markers aligned with surviving sources)

**Slack ingestion**
- Per-workspace OAuth connect — no shared bot token required
- Scheduled background ingestion (APScheduler)
- **Incremental sync** — per-channel watermarks, dedupe via stable keys
  (`slack:{channel_id}:{ts}`), `FORCE_REINGEST` escape hatch
- Thread expansion into single Markdown documents
- **Realtime Slack webhook** for live updates (HMAC-verified, optional)

**Gmail ingestion**
- Per-workspace Google OAuth connect — read-only scopes only
- Multiple Gmail connections per workspace (personal + shared mailbox)
- Label-based ingestion filtering
- Configurable message cap per run (`GMAIL_MAX_MESSAGES_PER_RUN`)

**Multi-user & multi-workspace**
- Supabase auth (email/password via Supabase SDK; JWT verified server-side)
- Every workspace is isolated: separate HydraDB sub-tenant, separate tokens
- Workspace switcher in the frontend

**LLM answering**
- OpenAI-compatible LLM wrapper (OpenAI, OpenRouter, Together, Groq, etc.)
- **Streaming responses** via Server-Sent Events
- **Multi-turn conversation memory** — last 6 turns sent for reference
  resolution (`he`, `that decision`, etc.)
- Mode-specific prompts: default / summary / decisions / action_items /
  who_said / exact / hybrid
- Grounded answers with sources cited as `[1]`, `[2]`, etc.

**Chat & saved answers**
- Chat sessions persisted in Supabase (survives page refresh)
- Saved answers persisted in Supabase (synced across devices)
- Export to Markdown / TXT with safe filenames

**Production hardening**
- Startup env validation — refuses to boot with missing required vars or
  placeholder secrets
- Structured JSON logging with request/correlation/user/workspace IDs
- Per-route rate limiting (auth, query, webhook, ingest buckets)
- Typed error hierarchy with consistent `{detail, error_type}` JSON shape
- Liveness + readiness probes with per-dependency latency tracking
- Optional Sentry integration

## Tech stack

- **Backend:** Python 3.11+, FastAPI + Uvicorn, `slack_sdk`, `supabase`,
  `PyJWT`, `google-auth-oauthlib`, `requests`, `openai`, `apscheduler`,
  `cachetools`, `dateparser`
- **Frontend:** React 18, Vite 5, Supabase JS client, `react-markdown`,
  `remark-gfm`
- **Data:** HydraDB (vector store), Supabase (auth, metadata, chat history)

## Architecture

```
Users ──► React frontend ──► FastAPI backend
              │  (Supabase JWT)       │
              │                       ├─► HydraDB recall ──► Cloud LLM ──► SSE stream
              │                       │
              │                       ├─► Slack ingestion ──► HydraDB upload
              │                       │      (per-workspace bot token, OAuth)
              │                       │
              │                       └─► Gmail ingestion ──► HydraDB upload
              │                              (per-workspace OAuth, read-only)
              │
              └──► Supabase (auth, workspaces, chat sessions, saved answers,
                             slack_installations, gmail_connections)
```

Ingestion pipeline detail:

```
Slack  ──►  ingestion/  ──►  HydraDB     ──►  recall.py  ──►  llm.py  ──►  FastAPI
Gmail  ──►  gmail_oauth ──►  (per-workspace         │            │
               │              sub-tenant)      rerank +      streaming
               │                               person/chan    (SSE)
          APScheduler                          inference
          + realtime
          webhook
```

## Folder structure

```
backend/
├── .env.example              # all env vars documented; copy → .env
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── DEPLOYMENT.md             # step-by-step deploy guide (Render / Railway)
├── LOGGING.md                # structured logging architecture
├── TEST_REPORT.md            # test coverage report + known issues
├── main.py                   # FastAPI app — routes, CORS, lifespan hooks
├── auth.py                   # X-API-Key dep (legacy admin route only)
├── auth_supabase.py          # Supabase JWT verification (HS256 + ES256/RS256)
├── supabase_client.py        # Supabase service-role client + all DB ops
├── slack_oauth.py            # per-workspace Slack OAuth flow + ingestion runner
├── gmail_oauth.py            # per-workspace Gmail OAuth flow + ingestion runner
├── slack_signature.py        # HMAC-SHA256 verification for /slack/events
├── realtime_ingest.py        # Slack Events API endpoint + background processing
├── scheduler.py              # APScheduler — periodic per-workspace ingestion
├── hydradb_client.py         # HydraDB ingestion + recall HTTP client
├── recall.py                 # HydraDB recall + grounded answer + reranking
├── llm.py                    # Cloud LLM wrapper (OpenAI-compatible) + streaming
├── prompts.py                # per-mode system prompts + conversation history fmt
├── query_rewriter.py         # person/channel inference heuristics
├── search_utils.py           # keyword extraction, rerank, metadata bias
├── date_utils.py             # natural-language date phrase parser
├── query_cache.py            # TTL cache for stateless queries
├── rate_limit.py             # per-bucket sliding-window rate limiter
├── errors.py                 # typed AppError hierarchy + global handler
├── startup.py                # env validation at boot (Phase 7 hardening)
├── logging_config.py         # structured JSON logging + ContextVar injection
├── observability.py          # Sentry, dead-letter logging, /api/ready checks
├── request_context.py        # ASGI middleware — request/correlation IDs
├── retry.py                  # exponential backoff with jitter (no tenacity)
├── data/
│   ├── .gitkeep
│   └── ingestion_state.json  # per-channel watermarks (gitignored)
├── supabase/
│   ├── schema.sql            # base tables + RLS policies
│   ├── phase2_chat_and_saved.sql
│   ├── phase3_slack_connect.sql
│   ├── phase4_hydradb_workspace_isolation.sql
│   ├── phase7_production_hardening.sql
│   └── phase8_gmail_connector.sql
└── ingestion/
    ├── slack_client.py       # Slack API wrapper with caching
    ├── normalize.py          # noise filtering, thread detection
    ├── ingestion_state.py    # JSON state file helpers
    └── ingest_slack.py       # CLI ingestion entry point (single-workspace)

frontend/
├── package.json
├── vite.config.js
├── index.html
├── .env.example
└── src/
    ├── main.jsx
    ├── App.jsx               # chat UI, panels, history, saved answers
    ├── api.js                # API client (askQuery, streamQuery, all endpoints)
    ├── styles.css            # light + dark theme, mobile-responsive
    ├── auth/
    │   ├── AuthContext.jsx   # Supabase session management
    │   ├── AuthForm.jsx      # sign-in / sign-up UI
    │   ├── AuthGate.jsx      # session guard wrapper
    │   ├── WorkspaceContext.jsx  # active workspace + token getter
    │   └── WorkspaceSwitcher.jsx
    ├── slack/
    │   └── SlackSettings.jsx # channel picker + connect / ingest UI
    ├── gmail/
    │   └── GmailSettings.jsx # Gmail connection manager + label picker
    └── lib/
        └── supabase.js       # Supabase client singleton
```

## Quick start

### Prerequisites

- Python 3.11+
- Node.js 18+
- A [Supabase](https://supabase.com) project (free tier is fine)
- HydraDB account (API key + tenant ID)
- OpenAI API key (or any OpenAI-compatible provider)
- A [Slack app](https://api.slack.com/apps) with OAuth configured (see below)

### 1 — Clone and set up the backend

```bash
git clone <your-repo-url>
cd <your-repo>/backend

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env             # then fill in the required values (see below)
```

### 2 — Set up Supabase

1. Create a new Supabase project.
2. In the SQL editor, run each migration in order:
   ```
   backend/supabase/schema.sql
   backend/supabase/phase2_chat_and_saved.sql
   backend/supabase/phase3_slack_connect.sql
   backend/supabase/phase4_hydradb_workspace_isolation.sql
   backend/supabase/phase7_production_hardening.sql
   backend/supabase/phase8_gmail_connector.sql
   ```
3. From **Settings → API**, copy `Project URL`, `anon/public key`, `service_role key`,
   and `JWT Secret` into `backend/.env`.
4. Create a workspace row in the `workspaces` table and add your user to
   `workspace_memberships`. (There is no UI for this yet — use the Supabase
   table editor or SQL.)

### 3 — Set up the frontend

```bash
cd ../frontend
npm install
cp .env.example .env.local      # set VITE_SUPABASE_URL, VITE_SUPABASE_ANON_KEY, VITE_API_BASE_URL
```

### 4 — Run locally

```bash
# Terminal 1 — backend
cd backend
uvicorn main:app --reload --port 8000

# Terminal 2 — frontend
cd frontend
npm run dev
```

Backend at `http://127.0.0.1:8000` (interactive docs at `/docs`).
Frontend at `http://localhost:5173`.

## Backend configuration

Copy `backend/.env.example` → `backend/.env` and fill in real values. The
app will refuse to start if any REQUIRED variable is missing or still set to
a placeholder.

### Required

| Key | Purpose |
|---|---|
| `HYDRADB_API_KEY` | HydraDB API key. |
| `HYDRADB_TENANT_ID` | HydraDB project-wide tenant ID. |
| `OPENAI_API_KEY` | LLM provider API key. |
| `SUPABASE_URL` | Your Supabase project URL (`https://xxx.supabase.co`). |
| `SUPABASE_JWT_SECRET` | JWT secret from Supabase → Settings → API. |
| `SUPABASE_SERVICE_ROLE_KEY` | Service-role key (never expose to the browser). |

### Slack OAuth (required to use the Slack connector)

| Key | Purpose |
|---|---|
| `SLACK_CLIENT_ID` | OAuth client ID from your Slack app. |
| `SLACK_CLIENT_SECRET` | OAuth client secret. |
| `SLACK_REDIRECT_URI` | Must match the Redirect URL registered in the Slack app. Local: `http://127.0.0.1:8000/api/slack/oauth/callback`. |
| `SLACK_OAUTH_STATE_SECRET` | Random 32-byte hex string used to HMAC-sign OAuth state tokens. |
| `SLACK_SIGNING_SECRET` | Signing secret from Slack app → Basic Information (required for realtime webhook). |

### Gmail OAuth (optional)

Leave these blank to disable the Gmail connector. The rest of the app boots
normally without them.

| Key | Purpose |
|---|---|
| `GMAIL_CLIENT_ID` | Google OAuth 2.0 client ID. |
| `GMAIL_CLIENT_SECRET` | Google OAuth 2.0 client secret. |
| `GMAIL_REDIRECT_URI` | Must match an Authorized Redirect URI in Google Cloud Console. Local: `http://127.0.0.1:8000/api/gmail/oauth/callback`. |
| `GMAIL_OAUTH_STATE_SECRET` | Separate random 32-byte hex string for Gmail state signing. |

### Optional tuning

| Key | Default | Purpose |
|---|---|---|
| `APP_API_KEY` | — | Shared secret for the legacy `/api/admin/status` route. |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Comma-separated allowed browser origins. |
| `FRONTEND_BASE_URL` | first CORS origin | Where OAuth callbacks redirect after completing. |
| `OPENAI_BASE_URL` | `https://api.openai.com` | Override for OpenRouter / Groq / Together / Azure. |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model name for the LLM. |
| `LLM_MAX_TOKENS` | `500` | Max LLM output tokens. |
| `HYDRADB_BASE_URL` | `https://api.hydradb.com` | Override HydraDB endpoint. |
| `HYDRADB_SUB_TENANT_ID` | `slack-second-brain` | Fallback sub-tenant for legacy CLI ingestion. |
| `AUTO_INGEST` | `false` | `true` to enable scheduled background ingestion. |
| `AUTO_INGEST_INTERVAL_MINUTES` | `15` | Minutes between scheduler runs. |
| `AUTO_INGEST_RUN_ON_STARTUP` | `false` | `true` to ingest immediately on boot. |
| `REALTIME_INGEST_ENABLED` | `true` | `false` to disable `POST /slack/events`. |
| `QUERY_CACHE_ENABLED` | `true` | `false` to disable the query cache. |
| `QUERY_CACHE_TTL_SECONDS` | `300` | Cache entry lifetime in seconds. |
| `QUERY_CACHE_MAX_SIZE` | `100` | LRU cap on cached queries. |
| `RATE_LIMIT_PER_5_MIN` | `20` | Query bucket limit per client per 5 minutes. |
| `RATE_LIMIT_AUTH_PER_5_MIN` | `30` | Auth bucket limit. |
| `RATE_LIMIT_SLACK_WEBHOOK_PER_5_MIN` | `600` | Webhook bucket limit. |
| `RATE_LIMIT_INGEST_PER_5_MIN` | `5` | Manual ingest trigger limit. |
| `GMAIL_MAX_MESSAGES_PER_RUN` | `100` | Max Gmail messages fetched per ingest run. |
| `GMAIL_ALLOW_SPAM_TRASH` | `false` | `true` to allow ingesting Spam/Trash labels. |
| `SLACK_LIMIT_PER_CHANNEL` | `500` | Messages pulled per channel per scheduler run. |
| `HYDRADB_BATCH_SIZE` | `50` | Files per HydraDB upload batch. |
| `FORCE_REINGEST` | `false` | `true` to skip dedupe state (CLI only). |
| `DEBUG_RECALL` | `false` | `true` to log raw HydraDB chunk responses. |
| `LOG_LEVEL` | `INFO` | `DEBUG \| INFO \| WARNING \| ERROR`. |
| `ENVIRONMENT` | `local` | Echoed in `/api/health` — `local \| staging \| production`. |
| `SENTRY_DSN` | — | Sentry DSN for error tracking (optional). |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.1` | Sentry trace sampling ratio (0.0–1.0). |

## Frontend configuration

Copy `frontend/.env.example` → `frontend/.env.local`:

| Key | Purpose |
|---|---|
| `VITE_SUPABASE_URL` | Your Supabase project URL (same as backend `SUPABASE_URL`). |
| `VITE_SUPABASE_ANON_KEY` | Supabase anon/public key (safe for the browser). |
| `VITE_API_BASE_URL` | Backend base URL, e.g. `http://localhost:8000` or `https://your-backend.onrender.com`. |
| `VITE_APP_API_KEY` | Legacy `APP_API_KEY` — only used by the admin status card. Leave blank in production. |

These values are baked into the JS bundle at build time (Vite static replacement).

## Slack app setup

1. Create a new app at https://api.slack.com/apps → **From scratch**.
2. Under **OAuth & Permissions → Bot Token Scopes**, add:
   - `channels:history`, `channels:read`
   - `groups:history`, `groups:read` (for private channels, optional)
   - `users:read`
3. Under **OAuth & Permissions → Redirect URLs**, add your `SLACK_REDIRECT_URI`.
4. Under **Basic Information**, copy the **Client ID**, **Client Secret**, and
   **Signing Secret** into your `.env`.
5. **Connect** from the frontend: open Settings → Slack → Connect Workspace.
   The OAuth flow installs the bot into your Slack workspace and stores the
   bot token in Supabase (per workspace; never returned to the browser).
6. **Select channels** in the Slack settings panel and click **Save**, then
   click **Ingest Now** to pull messages into HydraDB.

### Optional: realtime webhook

For messages to appear within seconds (instead of waiting for the scheduler):

1. Under **Event Subscriptions**, enable events and set the Request URL to
   `https://<your-host>/slack/events`.
2. Subscribe to bot events: `message.channels`, `message.groups`.
3. Ensure `SLACK_SIGNING_SECRET` is set and `REALTIME_INGEST_ENABLED=true`.

For local development you need a public tunnel (ngrok / cloudflared).

## Gmail app setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project and enable the **Gmail API**.
2. Under **APIs & Services → Credentials**, create an **OAuth 2.0 Client ID**
   (type: Web application).
3. Add your `GMAIL_REDIRECT_URI` to **Authorized redirect URIs**.
4. Copy the **Client ID** and **Client Secret** into your `.env`.
5. Generate `GMAIL_OAUTH_STATE_SECRET`:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
6. **Connect** from the frontend: open Settings → Gmail → Connect Account.
   Multiple Gmail accounts can be connected per workspace.

Scopes requested (read-only): `openid email profile gmail.readonly`.
The connector never sends, modifies, or deletes email.

## HydraDB setup

1. Sign up / log in at HydraDB and create a tenant.
2. Copy your **API key** and **tenant ID** into `HYDRADB_API_KEY` and
   `HYDRADB_TENANT_ID`.
3. Per-workspace sub-tenants are created automatically when a workspace first
   ingests. You do not need to pre-create them.

## Authentication

All `/api/*` user routes require a Supabase JWT in the `Authorization: Bearer <token>`
header. The frontend's Supabase client handles token acquisition and refresh automatically.

Routes that require a workspace also expect `X-Workspace-Id: <uuid>` — the frontend
sets this from `WorkspaceContext`.

OAuth callback routes (`/api/slack/oauth/callback`, `/api/gmail/oauth/callback`)
authenticate via HMAC-signed state tokens baked into the OAuth redirect URL; they
do not require a JWT.

The legacy `/api/admin/status` route uses `X-API-Key` (set `APP_API_KEY` in `.env`).

## API endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | public | Service info. |
| `GET` | `/api/health` | public | Liveness probe — `{status, service, environment, version}`. |
| `GET` | `/api/ready` | public | Readiness probe — checks Supabase, HydraDB, OpenAI. Returns 503 if any dep is down. |
| `GET` | `/api/me` | JWT | Current user info. |
| `GET` | `/api/me/workspaces` | JWT | List workspaces the user belongs to. |
| `POST` | `/api/query` | JWT + workspace | Ask a question; returns JSON answer + sources + debug. |
| `POST` | `/api/query/stream` | JWT + workspace | Same, streamed via SSE. |
| `GET` | `/api/chat/sessions` | JWT + workspace | List chat sessions. |
| `POST` | `/api/chat/sessions` | JWT + workspace | Create a chat session. |
| `GET` | `/api/chat/sessions/{id}/messages` | JWT + workspace | Get messages for a session. |
| `POST` | `/api/chat/sessions/{id}/messages` | JWT + workspace | Append a message. |
| `GET` | `/api/saved-answers` | JWT + workspace | List saved answers. |
| `POST` | `/api/saved-answers` | JWT + workspace | Save an answer. |
| `DELETE` | `/api/saved-answers/{id}` | JWT + workspace | Delete a saved answer. |
| `GET` | `/api/slack/connect-url` | JWT + workspace | Get the Slack OAuth authorization URL. |
| `GET` | `/api/slack/oauth/callback` | Signed state | OAuth redirect handler (Slack → backend). |
| `GET` | `/api/slack/channels` | JWT + workspace | List channels available in the connected workspace. |
| `POST` | `/api/slack/channels` | JWT + workspace | Save selected channels. |
| `POST` | `/api/slack/ingest` | JWT + workspace | Trigger a manual Slack ingest (background). |
| `GET` | `/api/gmail/connect-url` | JWT + workspace | Get the Gmail OAuth authorization URL. |
| `GET` | `/api/gmail/oauth/callback` | Signed state | OAuth redirect handler (Google → backend). |
| `GET` | `/api/gmail/connections` | JWT + workspace | List Gmail connections. |
| `DELETE` | `/api/gmail/connections/{id}` | JWT + workspace | Remove a Gmail connection. |
| `GET` | `/api/gmail/labels` | JWT + workspace | List labels for a connection. |
| `POST` | `/api/gmail/labels` | JWT + workspace | Save selected labels. |
| `POST` | `/api/gmail/ingest` | JWT + workspace | Trigger a manual Gmail ingest (background). |
| `GET` | `/api/admin/status` | `X-API-Key` | Ingestion + scheduler diagnostics (legacy). |
| `POST` | `/slack/events` | Slack HMAC | Realtime Slack event webhook (when enabled). |

### Query request shape

```json
{
  "question": "What did Rahul say about latency?",
  "top_k": 5,
  "mode": "default",
  "channel": null,
  "user": null,
  "document_type": null,
  "start_timestamp": null,
  "end_timestamp": null,
  "date_query": null,
  "conversation_history": []
}
```

- `mode` — `default | summary | decisions | action_items | who_said | exact | hybrid`
- `top_k` — integer 1–10 (default 5)
- `conversation_history` — last 6 `{role, content}` turns (oldest first)
- `date_query` — natural-language phrase (`last week`, `after May 10`); overridden
  by explicit `start_timestamp` / `end_timestamp`

### Streaming response

`POST /api/query/stream` returns `text/event-stream`:

- `event: token  data: {"text": "<delta>"}` — one or more per response
- `event: done   data: {"answer": ..., "sources": [...], "debug": {...}}` — final
- `event: error  data: {"detail": ..., "error_type": ...}` — on failure

### Sample non-streaming response

```json
{
  "answer": "Rahul said latency is fine in production [1].",
  "sources": [
    {
      "index": 1,
      "channel": "product",
      "user": "Rahul Verma",
      "timestamp": "1778775842.876209",
      "snippet": "Latency is fine in production. We measured it last week.",
      "permalink": "https://your-workspace.slack.com/archives/C012/p1778775842876209",
      "stable_key": "slack:C012:1778775842.876209",
      "document_type": "message"
    }
  ],
  "debug": {
    "cache_hit": false,
    "chunks_returned": 5,
    "chunks_used": 1,
    "retrieval_mode": "default",
    "query_rewrite": {
      "inferred_person": "Rahul",
      "person_confidence": "strong",
      "inferred_channel": null,
      "retrieval_biases_applied": ["person:strong"]
    }
  }
}
```

### Error responses

| Status | When |
|---|---|
| `401` | Missing or invalid JWT / API key. |
| `403` | Workspace not found or user is not a member. |
| `422` | Request body fails Pydantic validation. |
| `429` | Rate limit exceeded for this bucket. |
| `502` | Upstream LLM or HydraDB failure. |
| `503` | Required connector not configured (e.g. Gmail vars missing). |
| `504` | Upstream timeout. |

## Frontend usage

Once both backend and frontend are running:

1. Open `http://localhost:5173` and sign in (or create an account).
2. Select your workspace (or ask an admin to add you to one via Supabase).
3. Connect **Slack** and/or **Gmail** via the Settings panels in the header.
4. Type a question in the composer. Press **Ask** or Enter.
5. Watch the answer stream in. Click **Stop** to cancel mid-stream.
6. Each completed answer has:
   - **☆ Save** — bookmark this answer (persisted to Supabase)
   - **⬇ MD** / **⬇ TXT** — download answer + sources
   - **⧉ Copy** — copy answer text to clipboard
7. Source cards under each answer have **Open in Slack** or **Open in Gmail** + **⧉ Copy link**.
8. Header buttons: **☾ Dark / ☀ Light**, **History**, **Saved**, **Clear chat**.
9. Conversation memory works automatically — follow-up questions like
   "What did he say about X?" resolve correctly.

## Running tests

```bash
cd backend
pytest --tb=short -q
```

The CI coverage gate is **85%**. To check coverage locally:

```bash
pytest --cov=. --cov-report=term-missing
```

There are no frontend tests currently.

## Deployment

See [`backend/DEPLOYMENT.md`](backend/DEPLOYMENT.md) for step-by-step instructions
for Render + Railway (backend) and Vercel (frontend), including the production
checklist and Sentry setup.

## Local files that are not committed

- `backend/.env` — real secrets; copy from `.env.example` and fill in
- `frontend/.env.local` — frontend env; copy from `.env.example` and fill in
- `backend/data/ingestion_state.json` — regenerated on the next ingestion run
- `frontend/node_modules/`, `frontend/dist/`

## Notes

- The cloud LLM is required — there is no local-model fallback.
- Chat sessions and saved answers are persisted in Supabase (synced across
  devices and page refreshes). `localStorage` is used only as a local cache
  and fallback.
- Realtime webhook ingestion is optional; scheduled + incremental sync covers
  the common case.
- The CLI ingestion script (`python -m ingestion.ingest_slack`) is a legacy
  single-workspace path. Multi-workspace deployments use the per-workspace
  OAuth flow via the UI.
