# Second Brain (HydraDB MVP)

A lightweight "second brain" for your Slack workspace. It ingests selected
Slack channels and threads into HydraDB, retrieves relevant context for a
user's question, and asks a cloud LLM for a grounded answer with cited
Slack sources (channel, user, snippet, permalink). Backend only — built
to ship in a few days.

## Tech stack

- Python 3.11+
- FastAPI + Uvicorn
- `slack_sdk` for Slack APIs
- HydraDB (knowledge ingestion + recall)
- OpenAI Python SDK (works with any OpenAI-compatible endpoint such as
  OpenAI, OpenRouter, Together, Groq)

## Pipeline

```
Slack  ──►  ingestion  ──►  HydraDB  ──►  recall  ──►  Cloud LLM  ──►  FastAPI  ──►  Frontend
```

## Folder structure

```
backend/
├── .env.example
├── .gitignore
├── requirements.txt
├── main.py                   # FastAPI app (routes, CORS, auth)
├── auth.py                   # X-API-Key dependency
├── llm.py                    # Cloud LLM wrapper (OpenAI-compatible)
├── recall.py                 # HydraDB recall + grounded answer
├── hydradb_client.py         # HydraDB ingestion + recall HTTP client
├── data/
│   ├── .gitkeep
│   └── ingestion_state.json  # local dedupe state (gitignored)
└── ingestion/
    ├── __init__.py
    ├── slack_client.py       # Slack API wrapper with caches
    ├── normalize.py          # noise filtering, thread detection
    ├── ingestion_state.py    # JSON state file helpers
    └── ingest_slack.py       # CLI ingestion entry point
```

## Setup

```bash
git clone <your-repo-url>
cd <your-repo>/backend

python -m venv .venv
source .venv/bin/activate          # POSIX
# .venv\Scripts\Activate.ps1       # Windows PowerShell

pip install -r requirements.txt
cp .env.example .env               # then fill in real values
```

## Configuration

Copy `.env.example` to `.env` and fill in the values that apply to your
setup. The keys are:

| Key | Purpose |
|---|---|
| `APP_API_KEY` | Shared secret required in `X-API-Key` for `POST /api/query`. |
| `CORS_ORIGINS` | Comma-separated browser origins allowed to call the API. Defaults to `http://localhost:3000,http://localhost:5173`. |
| `SLACK_BOT_TOKEN` | Bot token from your Slack app (starts with `xoxb-…`). |
| `SLACK_CHANNEL_IDS` | Comma-separated Slack channel IDs to ingest. |
| `HYDRADB_API_KEY` | HydraDB API key. |
| `HYDRADB_TENANT_ID` | HydraDB tenant. |
| `HYDRADB_SUB_TENANT_ID` | Logical bucket inside the tenant; e.g. `slack-second-brain`. |
| `HYDRADB_BASE_URL` | Defaults to `https://api.hydradb.com`. |
| `OPENAI_API_KEY` | LLM provider API key. |
| `OPENAI_BASE_URL` | Leave blank for OpenAI; set for OpenRouter / Together / Groq / Azure-compatible gateways. |
| `OPENAI_MODEL` | Model name. Defaults to `gpt-4o-mini`. |
| `LLM_MAX_TOKENS` | Caps LLM output length. Defaults to `500`. |
| `DEBUG_RECALL` | `true` to print raw HydraDB responses to stdout. Defaults to `false`. |
| `SLACK_LIMIT_PER_CHANNEL` | Messages pulled per channel per run. Defaults to `500`. |
| `HYDRADB_BATCH_SIZE` | Files per HydraDB upload call. Defaults to `50`. |
| `FORCE_REINGEST` | `true` to ignore the local dedupe state. Defaults to `false`. |

A minimal `.env` for local dev looks like:

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

## HydraDB setup

1. Sign up / log in at HydraDB and create a tenant for this project.
2. Copy your **API key** and **tenant ID** from the dashboard.
3. Pick a `sub_tenant_id` — a logical bucket name inside the tenant.
   The MVP uses `slack-second-brain` by default; you can keep that or
   choose your own.
4. Set the values in `.env`:
   ```
   HYDRADB_API_KEY=...
   HYDRADB_TENANT_ID=...
   HYDRADB_SUB_TENANT_ID=slack-second-brain
   ```

## Ingest Slack into HydraDB

From the `backend/` directory:

```bash
python -m ingestion.ingest_slack
```

What this does, per run:

- Pulls up to `SLACK_LIMIT_PER_CHANNEL` messages per channel.
- Expands every thread parent into a single Markdown document with replies.
- Resolves user IDs to readable names and attaches Slack permalinks.
- Skips anything already recorded in `data/ingestion_state.json` so
  re-running is safe and cheap.
- Uploads new files to HydraDB via `POST /ingestion/upload_knowledge`.

### Force re-ingestion

If you want to re-upload everything (e.g. after changing the markdown
header format), set `FORCE_REINGEST=true` for that run.

**PowerShell:**

```powershell
$env:FORCE_REINGEST="true"
python -m ingestion.ingest_slack
Remove-Item Env:\FORCE_REINGEST
```

**POSIX shell:**

```bash
FORCE_REINGEST=true python -m ingestion.ingest_slack
```

Force mode still updates the state file after a successful upload, so
the next normal run goes back to skip-mode.

## Run the API

From `backend/`:

```bash
python -m uvicorn main:app --reload --port 8000
```

You should see a line like:

```
[main] CORS allowed origins: ['http://localhost:3000', 'http://localhost:5173']
```

Then the server is at `http://127.0.0.1:8000`. Interactive docs are at
`/docs`.

## API endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | public | Service info card. |
| `GET` | `/api/health` | public | `{"status": "ok", "service": "second-brain-api"}` |
| `POST` | `/api/query` | `X-API-Key` | Ask a question, get a grounded answer with Slack sources. |

### `POST /api/query`

Headers:

- `Content-Type: application/json`
- `X-API-Key: <APP_API_KEY>`

Request body:

```json
{
  "question": "What is the memory layer for the MVP?",
  "top_k": 5
}
```

Validation:

- `question` — required string, 3–2000 chars after whitespace is stripped.
- `top_k` — optional integer, 1–10, default 5.

Example request:

```bash
curl -X POST http://127.0.0.1:8000/api/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: change-me-dev-key" \
  -d '{"question": "What is the memory layer for the MVP?", "top_k": 5}'
```

Sample successful response:

```json
{
  "answer": "The memory layer is HydraDB [1].",
  "sources": [
    {
      "index": 1,
      "source": "all-second-brain",
      "channel": "all-second-brain",
      "channel_id": "C0123456789",
      "user": "Praveer Nema",
      "timestamp": "1778775842.876209",
      "snippet": "The MVP will ingest Slack threads, store them in HydraDB, and answer using cloud LLM recall.",
      "permalink": "https://your-workspace.slack.com/archives/C0123456789/p1778775842876209",
      "stable_key": "slack:C0123456789:1778775842.876209",
      "document_type": "message",
      "score": 0.91
    }
  ],
  "debug": {
    "chunks_returned": 5,
    "chunks_used": 5,
    "sources_before_clean": 5,
    "sources_after_clean": 1,
    "top_k": 5
  }
}
```

Error responses:

| Status | When |
|---|---|
| `401 {"detail": "Unauthorized"}` | Missing or wrong `X-API-Key`. |
| `422` (FastAPI validation envelope) | Body fails the validation rules above. |

## Local files that are not committed

The following are intentionally listed in `.gitignore` and **not**
committed to the repository:

- `.env` — your real secrets (Slack token, HydraDB key, LLM key, etc.).
- `data/ingestion_state.json` — local dedupe state, regenerated on the
  next ingestion run.

The `data/` directory itself is kept in git via `data/.gitkeep`. Use
`cp .env.example .env` and fill in your own values; never commit a
populated `.env`.

## Notes

- This is an MVP scoped at Slack-only ingestion + cloud-LLM answering.
  No real-time sync, no scheduler, no admin UI, no multi-source
  connectors.
- The cloud LLM is required — there is no local-model fallback.
- The frontend is a separate project. CORS is preconfigured for
  `http://localhost:3000` (Next.js) and `http://localhost:5173` (Vite);
  override via `CORS_ORIGINS`.

Realtime Slack Events support is available behind REALTIME_INGEST_ENABLED=true.
For normal local use, scheduled incremental sync is recommended.