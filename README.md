# ai-email-agent

Reads your Gmail inbox every 15 minutes, classifies each unread email with GPT-4o-mini, generates a draft reply for anything that needs one, and posts a structured digest to Slack — sorted by priority, never touching your email.

---

## How it works

```
Gmail (read-only) → classify → draft replies → Slack digest
```

1. **Fetch** — pulls up to N unread emails from your inbox (Gmail readonly scope only)
2. **Classify** — GPT-4o-mini assigns category, priority, confidence, and a one-line summary
3. **Filter** — drops spam / newsletters / receipts; surfaces only what matters
4. **Draft** — generates a ready-to-edit reply for anything flagged `requires_reply=True`
5. **Digest** — posts a single Slack message, sorted high → medium priority, with draft previews

---

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (the project uses `uv` for dependency management — `pip install` directly will not work as expected)
- A Google Cloud project with the Gmail API enabled and an OAuth 2.0 desktop client credential (`credentials.json`)
- An OpenAI API key — **set a hard spending limit in the OpenAI dashboard before connecting to a live inbox**
- A Slack incoming webhook URL pointing at the channel you want digests in
- (For deployment) A Railway account or equivalent container host

---

## Setup

**1. Clone and install dependencies**
```bash
git clone <repo>
cd ai-email-agent
uv sync
```

**2. Copy and fill in the environment file**
```bash
cp .env.example .env
# edit .env with your keys
```

**3. Place your Google credentials**

Download `credentials.json` from the Google Cloud Console (APIs & Services → Credentials → your OAuth 2.0 Client ID → Download JSON) and put it at `config/credentials.json`.

**4. Run Gmail OAuth once to mint the token**
```bash
uv run python -m src.gmail_client
```
A browser window opens. Log in, click Allow. This writes `config/token.json`. You only need to do this once per machine (the token refreshes automatically afterward).

**5. Verify the pipeline**
```bash
uv run python -m src.pipeline --run-once
```
Check Slack for the digest.

---

## Configuration

All config is loaded from `.env`. Everything except the two required secrets has a working default.

| Variable | Required | Default | What it does |
|---|---|---|---|
| `OPENAI_API_KEY` | ✅ | — | OpenAI key for classifier + draft generator |
| `SLACK_WEBHOOK_URL` | ✅ | — | Incoming webhook URL for the Slack digest |
| `GMAIL_CREDENTIALS_PATH` | | `config/credentials.json` | Path to your OAuth client secret file |
| `GMAIL_TOKEN_B64` | | — | Base64-encoded `token.json` for container/Railway deploys |
| `RESPONSE_TONE` | | `formal` | Draft tone: `formal` / `casual` / `brief` |
| `MAX_EMAILS_PER_RUN` | | `20` | Hard cap on emails fetched per run (1–100) — primary cost control |
| `MIN_PRIORITY_TO_SURFACE` | | `medium` | Only surface emails at this priority or higher |
| `SCHEDULE_INTERVAL_MINUTES` | | `15` | How often the scheduler fires (1–1440) |
| `LOG_LEVEL` | | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `ENV` | | `dev` | `dev` = colored console logs; `prod` = JSON lines |
| `PORT` | | `8080` | Health server port (Railway injects this automatically) |

---

## Running

**One-shot (run and exit)**
```bash
uv run python -m src.pipeline --run-once
```

**Scheduled (every `SCHEDULE_INTERVAL_MINUTES`)**
```bash
uv run python -m src.pipeline
```
Runs immediately on start, then on the interval. Press Ctrl-C to stop.

**Custom interval for testing**
```bash
uv run python -m src.pipeline --interval-hours 0.1   # every 6 minutes
```

---

## Docker

**Build**
```bash
docker build -t ai-email-agent .
```

**The token problem in containers**

Containers can't open a browser for OAuth. Mint the token locally first (setup step 4), then encode it and pass it in as an env var — see [Shipping your OAuth token](#shipping-your-oauth-token) below.

**Run**
```bash
docker run --env-file .env \
  -e GMAIL_TOKEN_B64="<base64-token>" \
  -p 8080:8080 \
  ai-email-agent
```

**Override to server mode**
```bash
docker run -e RUN_MODE=server -p 8080:8080 ai-email-agent
```

---

## Deployment (Railway)

1. `railway login`
2. `railway init` — link to this repo
3. Add env vars in the Railway dashboard — at minimum `OPENAI_API_KEY`, `SLACK_WEBHOOK_URL`, and `GMAIL_TOKEN_B64` (see below)
4. `railway up`

Railway builds the Dockerfile, injects `PORT`, and starts the service. The `/health` endpoint reports the last run's status and is suitable as a Railway health check.

### Shipping your OAuth token

Gmail OAuth requires a `token.json` on disk. Baking it into the image is a credential leak. Instead, encode it locally and paste it into Railway as `GMAIL_TOKEN_B64`:

```bash
# macOS
base64 -i config/token.json | pbcopy

# Linux
base64 -w 0 config/token.json
```

Paste the output into Railway (or any container env) as `GMAIL_TOKEN_B64`. The agent decodes it on startup and writes a local `token.json` inside the container. The env var is the source of truth — it overwrites any file already on disk.

---

## Health endpoint

When running in scheduled mode, a lightweight HTTP server listens on `PORT` (default 8080):

```
GET /health
```

Response:
```json
{
  "healthy": true,
  "last_run_status": "ok",
  "last_run_finished_at": 1751234567.89,
  "last_run_summary": {
    "fetched": 12,
    "processed": 9,
    "surfaced": 4,
    "filtered": 5,
    "drafted": 3,
    "sent": true,
    "errors": []
  }
}
```

Returns `200` when healthy or never-run; `503` when the last run failed.

---

## Observability

- All logs are structlog events. In `ENV=prod` they are JSON lines (one event per line, aggregator-friendly).
- Every pipeline run binds a `run_id` via `structlog.contextvars` — all log lines for a run share the same ID.
- `GET /health` returns the last run's full summary dict.
- Cost control: `MAX_EMAILS_PER_RUN` caps LLM calls per run; set a hard spending limit in the OpenAI dashboard as a second layer.

---

## Troubleshooting

**Slack digest shows 0 emails even though you have unread mail**

`MIN_PRIORITY_TO_SURFACE` defaults to `medium`. If the classifier scores everything as `low` (common for newsletters, receipts, and quiet FYI chains), they are filtered before Slack — the digest header will show `0 surfaced · N filtered`. Set `MIN_PRIORITY_TO_SURFACE=low` temporarily to verify the pipeline is processing at all, then tune back up.

**`token.json` error after 7 days: "invalid_grant" or "Token has been expired or revoked"**

This happens when your Google Cloud project's OAuth consent screen is in **Testing** mode. Google caps refresh tokens from test-mode apps to 7 days — after that the token is invalidated and auto-refresh fails. Fix: Google Cloud Console → APIs & Services → OAuth consent screen → publish the app (for personal use this is a self-review). Then re-run `uv run python -m src.gmail_client` to mint a fresh `token.json` and re-encode it for Railway.

**`ModuleNotFoundError` or wrong Python version**

This project uses `uv`. Do not use `pip install` or `python -m venv` directly — they create a separate environment without the project's dependencies. Always prefix commands with `uv run`. If pyenv is picking up the wrong Python, run `uv python pin 3.12` from the project root.

**`GMAIL_TOKEN_B64` is set but auth still fails in the container**

The env var overwrites `config/token.json` at import time — if auth still fails after that, the token itself is expired (see the 7-day issue above). Re-mint locally, re-encode, and update the Railway env var.

**Pipeline "runs" but nothing appears in Slack**

Check `curl <your-railway-url>/health`. If `last_run_status` is `crashed`, the scheduler caught an unhandled exception — check Railway logs with `railway logs`. If `last_run_status` is `ok` but `sent` is `false`, the Slack webhook itself failed; verify `SLACK_WEBHOOK_URL` is correct.

**APScheduler fires but the pipeline crashes silently (local)**

`curl localhost:8080/health` — if `last_run_status` is `crashed`, set `LOG_LEVEL=DEBUG` to see the full traceback. The most common cause is a missing or expired `OPENAI_API_KEY`.

**Slack webhook returns 400 / "invalid_payload"**

The Block Kit message probably exceeded Slack's 50-block limit. Lower `MAX_EMAILS_PER_RUN` or raise `MIN_PRIORITY_TO_SURFACE` to reduce the number of emails in the digest.

---

## Project structure

```
src/
  pipeline.py         # Orchestrator + APScheduler + health server
  gmail_client.py     # Gmail OAuth + email fetch/parse
  classifier.py       # GPT-4o-mini email classifier (Pydantic structured output)
  draft_generator.py  # Tone-aware reply draft generator
  slack_client.py     # Block Kit formatter + webhook sender
  logging_config.py   # structlog setup (dev=colors, prod=JSON)
  agent.py            # Core LLM client + retry (used by server.py)
  server.py           # HTTP server for RUN_MODE=server (/ask, /health)
config/
  settings.py         # Pydantic-settings typed config (all env vars live here)
  credentials.json    # Google OAuth client secret (gitignored)
  token.json          # Minted OAuth token (gitignored)
docs/
  architecture.md     # System diagram with trust boundaries
  user-guide.md       # Non-technical guide for the inbox owner
tests/
Dockerfile
pyproject.toml
```

---

## License

R4GB0Y
