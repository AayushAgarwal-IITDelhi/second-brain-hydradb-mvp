# Deployment Guide

This guide walks through deploying Second Brain to a production
environment:

- **Frontend** on **Vercel** (free tier is plenty for the MVP)
- **Backend** on **Render** OR **Railway** (free / hobby tier)
- **Supabase** for auth + Postgres (the same project you used locally)
- **Slack app** with production OAuth + Events URLs

It assumes Phases 1-5 are already implemented and tested locally.

---

## 1. Prerequisites

You need:

- A working local dev setup (the app boots, the tests pass).
- A **Supabase project** with the Phase 1-4 SQL migrations applied.
- A **HydraDB API key + tenant id** (per-workspace sub-tenants are
  derived automatically; see `backend/supabase/phase4_*.sql`).
- An **OpenAI key** (or any OpenAI-compatible provider).
- A **Slack app** registered at https://api.slack.com/apps.
- A **GitHub repo** with this codebase pushed to it.

---

## 2. Domain plan

Pick the two production URLs you'll use BEFORE you start. Slack and
Supabase both pin redirect URLs to exact strings.

| Component | Example URL                                         |
|-----------|-----------------------------------------------------|
| Frontend  | `https://second-brain.vercel.app`                   |
| Backend   | `https://second-brain-api.onrender.com`             |
|           | (or `https://second-brain-api.up.railway.app`)      |

Write them down. The rest of the guide refers to them as
`<FRONTEND_URL>` and `<BACKEND_URL>`.

---

## 3. Deploy the backend (Render OR Railway)

### Option A — Render

1. **Push the repo** to GitHub.
2. In Render: **New +** → **Web Service** → connect the repo.
3. Configure:

   | Field             | Value                                                              |
   |-------------------|--------------------------------------------------------------------|
   | Root Directory    | `backend`                                                          |
   | Runtime           | `Python 3`                                                         |
   | Build Command     | `pip install -r requirements.txt`                                  |
   | Start Command     | `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 2`         |
   | Health Check Path | `/api/health`                                                      |
   | Plan              | Starter (or Free for testing)                                      |

4. Add **Environment Variables** — copy from `backend/.env.example`,
   but with PRODUCTION values:

   ```
   APP_API_KEY=<long-random-string>
   HYDRADB_API_KEY=<your-hydradb-key>
   HYDRADB_TENANT_ID=<your-tenant-id>
   OPENAI_API_KEY=<your-openai-key>
   SUPABASE_URL=https://<your-project>.supabase.co
   SUPABASE_JWT_SECRET=<from-supabase-dashboard>
   SUPABASE_SERVICE_ROLE_KEY=<from-supabase-dashboard>
   SLACK_CLIENT_ID=<your-slack-app-client-id>
   SLACK_CLIENT_SECRET=<your-slack-app-client-secret>
   SLACK_SIGNING_SECRET=<your-slack-signing-secret>
   SLACK_OAUTH_STATE_SECRET=<long-random-string>
   SLACK_REDIRECT_URI=<BACKEND_URL>/api/slack/oauth/callback
   FRONTEND_BASE_URL=<FRONTEND_URL>
   CORS_ORIGINS=<FRONTEND_URL>
   ENVIRONMENT=production
   LOG_LEVEL=INFO
   ```

5. Click **Create Web Service**. Watch the build log; the first build
   takes ~3-5 minutes.

6. Once it's up, test the health probe:

   ```bash
   curl <BACKEND_URL>/api/health
   # -> {"status":"ok","service":"second-brain-api","environment":"production","version":"<commit-sha>"}
   ```

### Option B — Railway

1. **New Project** → **Deploy from GitHub repo**.
2. Pick the backend folder via **Root Directory** = `backend`.
3. Railway auto-detects Python; if it doesn't, add a
   **`backend/Procfile`** containing:

   ```
   web: uvicorn main:app --host 0.0.0.0 --port $PORT --workers 2
   ```

4. Under **Variables**, paste the same env vars listed above.
5. Generate a domain via **Settings** → **Networking** →
   **Generate Domain**. That domain becomes `<BACKEND_URL>`.
6. Verify with `curl <BACKEND_URL>/api/health`.

---

## 4. Deploy the frontend (Vercel)

1. **Add New Project** → import the GitHub repo.
2. Configure:

   | Field            | Value      |
   |------------------|------------|
   | Framework Preset | Vite       |
   | Root Directory   | `frontend` |
   | Build Command    | `npm run build` (default) |
   | Output Directory | `dist`     (default)      |

3. **Environment Variables** (set for both Production AND Preview):

   ```
   VITE_API_BASE_URL=<BACKEND_URL>
   VITE_SUPABASE_URL=https://<your-project>.supabase.co
   VITE_SUPABASE_ANON_KEY=<from-supabase-dashboard>
   ```

   Leave `VITE_APP_API_KEY` BLANK in production — anything in
   `VITE_*` is shipped to the browser bundle, so admin-key access
   should not be exposed there.

4. **Deploy**. The first build takes ~1-2 minutes.

5. Once it's up, open `<FRONTEND_URL>` in your browser, sign in via
   Supabase, and confirm the Workspace switcher loads.

---

## 5. Wire up the Slack app to production

At https://api.slack.com/apps → your app:

### 5a. OAuth & Permissions

- Set **Redirect URLs** to include EXACTLY:
  ```
  <BACKEND_URL>/api/slack/oauth/callback
  ```
  (Local dev `http://127.0.0.1:8000/api/slack/oauth/callback` can
  coexist as a second entry.)

- Verify the **Bot Token Scopes**:
  - `channels:history`
  - `channels:read`
  - `groups:history`
  - `groups:read`
  - `users:read`

### 5b. Event Subscriptions

- Toggle **Enable Events** → **On**.
- **Request URL**:
  ```
  <BACKEND_URL>/slack/events
  ```
  Slack will POST a `url_verification` challenge to this URL; the
  backend handles it automatically and the field should turn green.

- Under **Subscribe to bot events**, add:
  - `message.channels` (public-channel messages)
  - `message.groups`   (private-channel messages, if you use them)

- **Save Changes**. Slack will re-verify the URL.

### 5c. Re-install the app to your Slack workspace

Because the scopes / event subscriptions changed, Slack invalidates
the bot token. Either:

- Click **Reinstall to Workspace** in the Slack app settings, or
- In the Second Brain UI: open the **Slack** panel → **Reconnect**.

---

## 6. Configure Supabase for production

In the Supabase dashboard:

1. **Authentication → URL Configuration**:

   - **Site URL**: `<FRONTEND_URL>`
   - **Redirect URLs** (one per line):
     ```
     <FRONTEND_URL>
     <FRONTEND_URL>/*
     ```

2. **Apply the migrations** if you haven't already, in order:

   ```
   backend/supabase/schema.sql
   backend/supabase/phase2_chat_and_saved.sql
   backend/supabase/phase3_slack_connect.sql
   backend/supabase/phase4_hydradb_workspace_isolation.sql
   ```

   Open each in the SQL Editor and run it. They're idempotent —
   safe to re-run if you're unsure which have been applied.

3. **Verify the signup trigger** is attached (it stamps the per-
   workspace `hydradb_sub_tenant_id` at user signup):

   ```sql
   select tgname from pg_trigger where tgname = 'on_auth_user_created';
   ```

   Should return one row.

---

## 7. Smoke test the full deployment

1. Visit `<FRONTEND_URL>` in an incognito window.
2. Sign up with a fresh email (Supabase will email a magic link).
3. After signing in:
   - The Workspace switcher should show one workspace (auto-created
     by the Phase 1 signup trigger).
   - The **Slack** panel should show a working **Connect Slack**
     button.
4. Click **Connect Slack** → approve in Slack → you should be
   redirected back to `<FRONTEND_URL>/?slack_connect=ok&reason=<team>`.
5. In the channel picker, select one channel → **Save channels** →
   **Run ingest**.
6. Wait ~10 seconds (or up to a minute depending on channel size),
   then ask a question in the chat composer. You should see grounded
   sources from that channel.
7. Post a new message in the Slack channel and re-ask within
   ~5 seconds — Phase 5 realtime ingest should have picked it up.

If any step fails, check:

- **Render/Railway logs**  — JSON logs with `event` fields.
- **Browser devtools network tab** — 401 means auth/CORS;
  403 means workspace membership; 502 means a backend dep failed.
- **Slack app event dashboard** — shows the HTTP status of each
  webhook delivery; a flood of 401s means `SLACK_SIGNING_SECRET`
  is wrong.

---

## 8. Production checklist

- [ ] `.env` is NOT committed (the repo's `.gitignore` blocks it).
- [ ] Secrets live in the host's secret manager, not in any file.
- [ ] `CORS_ORIGINS` on the backend includes ONLY your frontend
      origins (no wildcards).
- [ ] `SLACK_REDIRECT_URI` matches a Redirect URL registered in
      the Slack app exactly.
- [ ] `FRONTEND_BASE_URL` is set on the backend (controls where
      the OAuth callback bounces users to after Connect Slack).
- [ ] `SLACK_OAUTH_STATE_SECRET` is a long random string and is
      DIFFERENT from `SUPABASE_JWT_SECRET`.
- [ ] `VITE_APP_API_KEY` is BLANK in the Vercel project.
- [ ] `/api/health` returns 200 from the public internet.
- [ ] `/api/admin/status` returns 401 without `X-API-Key`
      (i.e. the admin key is not blank).
- [ ] Supabase `Site URL` matches `<FRONTEND_URL>`.
- [ ] Slack Event Request URL is verified (green check in dashboard).
- [ ] First end-to-end Ask → Answer round-trip succeeds.

---

## 9. Maintenance notes

- **Logs**: Render and Railway both expose live JSON logs in the
  dashboard. Filter by the `event` field (e.g.
  `event=workspace_ingest_complete`) to find specific operations.
- **Rolling updates**: pushing to your main branch triggers a rebuild
  on both Vercel and Render/Railway. Vercel keeps the old deploy
  serving until the new one passes its health check.
- **Rolling back**: each Vercel deploy gets a unique URL; you can
  promote any prior deploy to production from the Deployments tab.
  Render keeps the previous deploy and lets you redeploy it manually.
- **Scaling**: the backend is mostly I/O-bound (Slack + HydraDB +
  OpenAI). Bumping workers from 2 to 4 helps before you need a
  bigger machine. Realtime ingest serializes on a single in-process
  lock today; moving that to Redis is the right next step if you
  exceed ~10 active workspaces.
- **Cost**: the free tiers of Render + Vercel + Supabase comfortably
  cover the MVP. HydraDB and OpenAI usage are the dominant costs
  once you have real traffic.