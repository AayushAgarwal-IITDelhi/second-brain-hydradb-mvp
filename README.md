# Second Brain

A lightweight "second brain" for your Slack workspace. It ingests
selected Slack channels and threads into HydraDB, retrieves relevant
context for a user's question, and asks a cloud LLM for a grounded
answer with cited Slack sources. Comes with a React + Vite chat
frontend for streaming Q&A, multi-turn conversations, saved answers,
exports, and dark mode.

## Features

**Search & retrieval**
- HydraDB semantic recall with local re-ranking
- **Exact mode** — prefer chunks containing literal keyword matches
- **Hybrid mode** — combine semantic + keyword scoring
- **Person/channel-aware query rewriting** — "what did Rahul say in
  product?" automatically infers `user=Rahul`, `channel=product` and
  applies them as hard filters (strong inference) or ranking bias
  (weak inference)
- **Natural-language date parsing** — `last week`, `yesterday`,
  `after May 10`, `from May 1 to May 7`, etc. parsed server-side
- **Query TTL cache** with cache-bypass when conversation history is
  present
- Citation cleanup (`[N]` markers aligned with surviving sources)

**Slack ingestion**
- Scheduled background ingestion (APScheduler)
- **Incremental sync** — per-channel watermarks, dedupe via stable
  keys (`slack:{channel_id}:{ts}`), `FORCE_REINGEST` escape hatch
- Thread expansion into single Markdown documents
- Optional **realtime Slack webhook** for live updates (HMAC-verified,
  off by default)

**LLM answering**
- OpenAI-compatible LLM wrapper (works with OpenAI, OpenRouter, Together,
  Groq, etc.)
- **Streaming responses** via Server-Sent Events
- **Multi-turn conversation memory** — last 6 turns sent with each
  query for reference resolution (`he`, `that decision`, etc.)
- Mode-specific system prompts (default / summary / decisions /
  action_items / who_said / exact / hybrid)
- Grounded answers with sources cited as `[1]`, `[2]`, etc.

**API**
- FastAPI backend with `X-API-Key` auth
- Per-IP rate limiting (sliding 5-minute window)
- Typed errors (`429`, `502`, `504`) with normalized JSON shapes
- Startup environment validation

**Frontend (React + Vite)**
- Streaming chat UI with stop/cancel button
- **Markdown answer rendering** (react-markdown + remark-gfm —
  headings, lists, tables, inline + fenced code, links, GFM features)
- **Source cards** with channel, user, timestamp, snippet, and "Open
  in Slack →" permalink
- **Query history** (last 30 queries, persisted in localStorage,
  filterable, click to reload, "Run again" button)
- **Saved answers** (up to 50 bookmarked answers, persisted in
  localStorage, filterable, full-screen overlay view)
- **Export to Markdown / TXT** for any completed answer or saved item,
  with safe filenames (`second-brain-answer-YYYY-MM-DD-HH-mm.{md,txt}`)
- **Copy buttons** for answer text and source permalinks
- **Dark mode** (auto-detects `prefers-color-scheme`, persists choice)
- **Debug chips** showing inferred person/channel, retrieval mode,
  cache status, date-phrase resolution, and exact-match counts
- Mobile-responsive layout

## Tech stack

- **Backend:** Python 3.11+, FastAPI + Uvicorn, `slack_sdk`, HydraDB,
  OpenAI Python SDK (any OpenAI-compatible endpoint), APScheduler,
  `cachetools`, `dateparser`
- **Frontend:** React 18, Vite 5, react-markdown, remark-gfm

## Pipeline

```
Slack  ──►  ingestion  ──►  HydraDB  ──►  recall  ──►  Cloud LLM  ──►  FastAPI  ──►  React frontend
   │            │              │            │            │             │
   │            │              │            │       streaming      X-API-Key,
   │            │              │            │       (SSE)         rate-limit,
   │            │              │            │                     cache, CORS
   │            │              │       rerank +
   │            │              │       person/channel
   │            │              │       inference
   │            │       multipart
   │            │       upload
   │       incremental + dedupe
   │       (per-channel watermarks)
realtime
webhook
(optional)
```

## Folder structure

```
backend/
├── .env.example
├── requirements.txt
├── main.py                   # FastAPI app, routes, CORS, auth, rate limit
├── auth.py                   # X-API-Key dependency
├── llm.py                    # Cloud LLM wrapper (OpenAI-compatible) + streaming
├── recall.py                 # HydraDB recall + grounded answer + reranking
├── prompts.py                # Per-mode system prompts + conversation history fmt
├── query_rewriter.py         # Person/channel inference heuristics
├── search_utils.py           # Keyword extraction, rerank, metadata bias
├── date_utils.py             # Natural-language date phrase parser
├── query_cache.py            # TTL cache for stateless queries
├── rate_limit.py             # Per-IP sliding-window rate limit
├── scheduler.py              # APScheduler — periodic Slack ingestion
├── slack_signature.py        # HMAC verification for realtime webhook
├── realtime_ingest.py        # Optional Slack Events API endpoint
├── hydradb_client.py         # HydraDB ingestion + recall HTTP client
├── errors.py                 # Typed AppError + global handler
├── startup.py                # Env validation at boot
├── data/
│   ├── .gitkeep
│   └── ingestion_state.json  # local dedupe state (gitignored)
└── ingestion/
    ├── slack_client.py       # Slack API wrapper with caches
    ├── normalize.py          # noise filtering, thread detection
    ├── ingestion_state.py    # JSON state file helpers
    └── ingest_slack.py       # CLI ingestion entry point

frontend/
├── package.json
├── vite.config.js
├── index.html
├── .env.example
└── src/
    ├── main.jsx
    ├── App.jsx               # Chat UI, panels, overlay, theming
    ├── api.js                # askQuery, streamQuery (SSE), admin status
    └── styles.css            # Light + dark theme, mobile-responsive
```

## Setup

### Backend

```bash
git clone <your-repo-url>
cd <your-repo>/backend

python -m venv .venv
source .venv/bin/activate          # POSIX
# .venv\Scripts\Activate.ps1       # Windows PowerShell

pip install -r requirements.txt
cp .env.example .env               # then fill in real values
```

### Frontend

```bash
cd ../frontend
npm install
cp .env.example .env               # set VITE_API_BASE_URL + VITE_APP_API_KEY
npm run dev
```

The frontend dev server runs at `http://localhost:5173` and talks to the
backend at `VITE_API_BASE_URL` (default `http://127.0.0.1:8000`).

## Configuration

Copy `backend/.env.example` to `backend/.env` and fill in the values:

| Key | Purpose |
|---|---|
| `APP_API_KEY` | Shared secret required in `X-API-Key` for all `/api/*` routes. |
| `CORS_ORIGINS` | Comma-separated browser origins. Defaults to `http://localhost:3000,http://localhost:5173`. |
| `RATE_LIMIT_PER_5_MIN` | Per-IP query limit. Defaults to `30`. |
| `QUERY_CACHE_ENABLED` | `true` to cache stateless query responses. Defaults to `true`. |
| `QUERY_CACHE_TTL_SECONDS` | Cache entry lifetime. Defaults to `300`. |
| `QUERY_CACHE_MAX_SIZE` | LRU cap. Defaults to `256`. |
| `SLACK_BOT_TOKEN` | Bot token from your Slack app (starts with `xoxb-…`). |
| `SLACK_CHANNEL_IDS` | Comma-separated Slack channel IDs to ingest. |
| `SLACK_SIGNING_SECRET` | Required only if you enable the realtime webhook. |
| `REALTIME_INGEST_ENABLED` | `true` to mount `POST /slack/events`. Defaults to `false`. |
| `HYDRADB_API_KEY` | HydraDB API key. |
| `HYDRADB_TENANT_ID` | HydraDB tenant. |
| `HYDRADB_SUB_TENANT_ID` | Logical bucket; e.g. `slack-second-brain`. |
| `HYDRADB_BASE_URL` | Defaults to `https://api.hydradb.com`. |
| `OPENAI_API_KEY` | LLM provider API key. |
| `OPENAI_BASE_URL` | Blank for OpenAI; set for OpenRouter / Together / Groq / Azure-compatible gateways. |
| `OPENAI_MODEL` | Model name. Defaults to `gpt-4o-mini`. |
| `LLM_MAX_TOKENS` | Caps LLM output length. Defaults to `500`. |
| `DEBUG_RECALL` | `true` to print raw HydraDB responses to stdout. Defaults to `false`. |
| `AUTO_INGEST` | `true` to run scheduled background ingestion. Defaults to `false`. |
| `AUTO_INGEST_INTERVAL_MINUTES` | Interval between scheduled runs. Defaults to `30`. |
| `AUTO_INGEST_RUN_ON_STARTUP` | `true` to ingest immediately on boot. Defaults to `false`. |
| `SLACK_LIMIT_PER_CHANNEL` | Messages pulled per channel per run. Defaults to `500`. |
| `HYDRADB_BATCH_SIZE` | Files per HydraDB upload call. Defaults to `50`. |
| `FORCE_REINGEST` | `true` to ignore the local dedupe state. Defaults to `false`. |

A minimal `.env` for local dev:

```bash
APP_API_KEY=change-me-dev-key
CORS_ORIGINS=http://localhost:3000,http://localhost:5173

SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_IDS=C0123456789,C9876543210

HYDRADB_API_KEY=hdb-...
HYDRADB_TENANT_ID=your-tenant-id
HYDRADB_SUB_TENANT_ID=slack-second-brain

OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=
OPENAI_MODEL=gpt-4o-mini
```

## Slack app setup

1. Create a new app at https://api.slack.com/apps → **From scratch**.
2. Under **OAuth & Permissions → Bot Token Scopes**, add:
   - `channels:history` — read messages in public channels
   - `channels:read` — resolve channel names
   - `groups:history` — read messages in private channels (optional)
   - `groups:read` — resolve private channel names (optional)
   - `users:read` — resolve `U…` user IDs to readable names
3. **Install to workspace** to get the bot token (`xoxb-…`). Set it as
   `SLACK_BOT_TOKEN` in your `.env`.
4. **Invite the bot to every channel** you plan to ingest. In Slack:
   ```
   /invite @your-bot-name
   ```
5. **Find each channel ID**: in Slack, right-click the channel → **View
   channel details** → scroll to the bottom; the ID looks like
   `C0123456789`. Paste a comma-separated list into `SLACK_CHANNEL_IDS`.

### Optional: realtime webhook

If you want fresh Slack messages to appear within seconds (instead of
waiting for the next scheduled ingest):

1. Under **Event Subscriptions**, enable events and point the Request
   URL at `https://<your-host>/slack/events`.
2. Subscribe to bot events: `message.channels`, `message.groups` as
   needed.
3. Set `SLACK_SIGNING_SECRET` and `REALTIME_INGEST_ENABLED=true`.

For local development you'll need a public tunnel (ngrok / cloudflared).
The webhook is optional — scheduled + incremental sync is the default.

## HydraDB setup

1. Sign up / log in at HydraDB and create a tenant for this project.
2. Copy your **API key** and **tenant ID** from the dashboard.
3. Pick a `sub_tenant_id` — a logical bucket name inside the tenant.
   The default is `slack-second-brain`.
4. Set the values in `.env`:
   ```
   HYDRADB_API_KEY=...
   HYDRADB_TENANT_ID=...
   HYDRADB_SUB_TENANT_ID=slack-second-brain
   ```

## Ingest Slack into HydraDB

### One-shot, from the CLI

```bash
cd backend
python -m ingestion.ingest_slack
```

Per run:
- Pulls up to `SLACK_LIMIT_PER_CHANNEL` messages per channel.
- Expands every thread parent into a single Markdown document with replies.
- Resolves user IDs to readable names and attaches Slack permalinks.
- Skips anything already in `data/ingestion_state.json` (per-channel
  watermarks, dedupe by stable key).
- Uploads new files to HydraDB.

### Scheduled (recommended)

Set `AUTO_INGEST=true` in `.env` and start the API — APScheduler will
run ingestion every `AUTO_INGEST_INTERVAL_MINUTES` minutes. Set
`AUTO_INGEST_RUN_ON_STARTUP=true` to run immediately on boot.

### Force re-ingestion

To re-upload everything (e.g. after changing the markdown header
format), set `FORCE_REINGEST=true` for one run:

```bash
FORCE_REINGEST=true python -m ingestion.ingest_slack
```

Force mode still updates the state file after a successful upload, so
the next normal run goes back to skip-mode.

## Run the API

```bash
cd backend
python -m uvicorn main:app --reload --port 8000
```

The server runs at `http://127.0.0.1:8000`. Interactive docs at `/docs`.

## API endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | public | Service info card. |
| `GET` | `/api/health` | public | `{"status": "ok", "service": "second-brain-api"}` |
| `POST` | `/api/query` | `X-API-Key` | Ask a question, get a grounded answer. JSON response. |
| `POST` | `/api/query/stream` | `X-API-Key` | Same request, streamed via Server-Sent Events. |
| `GET` | `/api/admin/status` | `X-API-Key` | Ingestion + scheduler diagnostics. |
| `POST` | `/slack/events` | HMAC | Optional realtime webhook (when enabled). |

### Request shape (both `/api/query` and `/api/query/stream`)

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

- `mode` — one of `default`, `summary`, `decisions`, `action_items`,
  `who_said`, `exact`, `hybrid`. Defaults to `default`.
- `top_k` — integer 1–10. Defaults to 5.
- `conversation_history` — last 6 `{role, content}` turns (oldest
  first). Used for reference resolution; never affects retrieval.
- `date_query` — natural-language phrase ("last week", "yesterday",
  "after May 10"). Resolved server-side. Explicit `start_timestamp` /
  `end_timestamp` override the parsed range.
- Person/channel inference runs on the question text automatically —
  no client-side hint needed.

### Streaming response

`POST /api/query/stream` returns `text/event-stream`. Event types:

- `event: token  data: { "text": "<delta>" }` — one or more per response
- `event: done   data: { "answer": ..., "sources": [...], "debug": {...} }` — final
- `event: error  data: { "detail": ..., "error_type": ... }` — on failure

### Example: streaming with curl

```bash
curl -N -X POST http://127.0.0.1:8000/api/query/stream \
  -H "Content-Type: application/json" \
  -H "X-API-Key: change-me-dev-key" \
  -d '{"question": "What did Rahul say about latency?", "mode": "default"}'
```

### Sample successful (non-streaming) response

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
    "chunks_returned": 5,
    "chunks_used": 1,
    "retrieval_mode": "default",
    "exact_matches_found": 0,
    "cache_hit": false,
    "top_k": 5,
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
| `401 {"detail": "Unauthorized"}` | Missing or wrong `X-API-Key`. |
| `422` | Body fails Pydantic validation. |
| `429 {"detail": "Rate limit exceeded"}` | Too many requests per IP. |
| `502` | Upstream LLM or HydraDB failure. |
| `504` | Upstream timeout. |

## Frontend usage

Once both backend and frontend are running:

1. Open `http://localhost:5173`.
2. Type a question in the composer at the bottom. Press **Ask** or Enter.
3. Watch the answer stream in. Click **Stop** to cancel mid-stream.
4. Each completed answer has:
   - **☆ Save** — bookmark this answer
   - **⬇ MD** / **⬇ TXT** — download the answer + sources
   - **⧉ Copy** — copy the answer text to clipboard
5. Source cards under each answer have **Open in Slack** + **⧉ Copy link**.
6. Header buttons:
   - **☾ Dark / ☀ Light** — toggle theme (persists)
   - **History** — recent queries with filter + "Run again"
   - **Saved** — bookmarked answers with filter + overlay view
   - **Clear chat** — two-click confirm for safety
7. Filters in the composer:
   - Mode dropdown (default / summary / decisions / action_items /
     who_said / exact / hybrid)
   - Optional channel / user / document type
   - Date phrase ("last week") or explicit start/end picker
8. Conversation memory works automatically — follow-up questions
   like "What did he say about X?" resolve correctly.

## Local files that are not committed

The following are in `.gitignore` and **not** committed:

- `backend/.env` — your real secrets
- `frontend/.env` — your frontend env (API base URL, API key)
- `backend/data/ingestion_state.json` — regenerated on the next
  ingestion run
- `frontend/node_modules/`
- `frontend/dist/`

Use `cp .env.example .env` and fill in your own values; never commit
a populated `.env`.

## Notes

- The cloud LLM is required — there is no local-model fallback.
- The frontend stores history and saved answers in `localStorage`;
  none of it is sent to the backend (multi-turn `conversation_history`
  is sent per-request but not persisted server-side).
- Realtime webhook ingestion is fully optional; scheduled + incremental
  sync covers the common case.