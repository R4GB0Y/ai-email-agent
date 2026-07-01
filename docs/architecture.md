# Architecture

## System diagram

```
╔═══════════════════════════════════════════════════════════════════════════╗
║  MY PROCESS  (you control this; trust everything inside)                 ║
║                                                                           ║
║   ┌───────────────┐   fires every                                         ║
║   │  APScheduler  │─────15 min──────────────────────────────┐            ║
║   └───────────────┘                                         ↓            ║
║                                                  ┌─────────────────────┐ ║
║                                                  │    pipeline.py      │ ║
║                                                  │   (orchestrator)    │ ║
║                                                  └──────────┬──────────┘ ║
║                                                             │            ║
║            Stage 1: fetch                                   │            ║
║         ┌───────────────────────────────────────────────────┤            ║
║         ↓                                                   │            ║
║  ┌──────────────────┐   emails[]                            │            ║
║  │  gmail_client.py │──────────────────────────────→        │            ║
║  └──────────────────┘                                       │            ║
║         ↑                                                   │            ║
║         │ OAuth token (config/token.json)                   │            ║
║         │ or GMAIL_TOKEN_B64 (containers)                   │            ║
║                                                             │            ║
║            Stage 2: classify + filter                       │            ║
║         ┌───────────────────────────────────────────────────┘            ║
║         ↓                                                                 ║
║  ┌──────────────────┐   classified[]                                      ║
║  │  classifier.py   │─────────────────────────────────────────┐          ║
║  └──────────────────┘                                         │          ║
║   drops: spam / newsletter / receipt                          │          ║
║   drops: priority < MIN_PRIORITY_TO_SURFACE                   │          ║
║                                                               │          ║
║            Stage 3: draft                                     │          ║
║                                                               ↓          ║
║                                                   ┌───────────────────┐  ║
║                                                   │ draft_generator.py│  ║
║                                                   └─────────┬─────────┘  ║
║                                                             │            ║
║            Stage 4: Slack                    enriched[]     │            ║
║                                                             ↓            ║
║                                                   ┌───────────────────┐  ║
║                                                   │  slack_client.py  │  ║
║                                                   └─────────┬─────────┘  ║
║                                                             │            ║
║   ┌──────────────────────┐                                  │            ║
║   │  /health  :8080      │  ← reports last run status       │            ║
║   │  (daemon thread)     │                                  │            ║
║   └──────────────────────┘                                  │            ║
║                                                             │            ║
╚═════════════════════════════════════════════════════════════╪════════════╝
                              TRUST BOUNDARY                  │
  ═══════════════════════════════════════════════════════════╪════════════
  External services you do NOT control:                       │
                                                             │
  ┌─────────────────┐     ┌──────────────────┐     ┌─────────↓──────────┐
  │   Gmail API     │     │   OpenAI API     │     │   Slack webhook    │
  │  (Google)       │     │  gpt-4o-mini     │     │  (Slack)           │
  │                 │     │                  │     │                    │
  │  read-only      │     │  classifier      │     │  one-way POST      │
  │  OAuth scope    │     │  + draft gen     │     │  no read-back      │
  └─────────────────┘     └──────────────────┘     └────────────────────┘
```

---

## Data shapes at each stage boundary

```
gmail_client  →  classifier
{
  subject:       str
  sender:        str
  date:          str
  snippet:       str          # Google's 100-char auto-summary
  body_preview:  str          # first 500 chars of decoded body
}

classifier  →  draft_generator  (adds classification key)
{
  ...email fields...,
  classification: ClassifiedEmail {
    category:           "action_needed" | "fyi" | "calendar" | ...
    priority:           "high" | "medium" | "low"
    confidence:         0.0–1.0
    requires_reply:     bool
    one_line_summary:   str (≤120 chars)
    suggested_deadline: "2025-07-04" | null
    reasoning:          str
  }
}

draft_generator  →  slack_client  (adds draft key)
{
  ...email + classification...,
  draft: DraftResponse | null    # null if requires_reply=False
  {
    subject_line:        str
    draft_body:          str
    tone_used:           "formal" | "casual" | "brief"
    word_count:          int
    key_points_covered:  list[str]
    confidence:          float
    human_review_note:   str | null
  }
}
```

---

## Error degradation paths

The pipeline is designed to degrade gracefully rather than fail silently:

```
Gmail fetch fails
  └─ return early, send no Slack message, record error in summary

Classifier fails for one email
  └─ skip that email, continue with the rest

Classifier fails entirely
  └─ fall through to Slack with empty enriched_emails list (sends "inbox zero" message)

Draft generator fails for one email
  └─ draft=None for that email, still surfaces it in Slack without a draft preview

Draft generator fails entirely
  └─ send digest without any drafts (all draft fields null)

Slack webhook fails (transient)
  └─ tenacity retries 4×: 1s, 2s, 4s, 8s
  └─ if still failing: log error, summary["sent"]=False, /health returns 503
```

---

## Trust boundary notes

**Inside the boundary (you control):**
- All Python processes
- `config/token.json` and `config/credentials.json` on disk
- The `GMAIL_TOKEN_B64` env var in your container

**Outside the boundary (you don't control):**
- **Gmail API**: Google can revoke tokens, change rate limits, or deprecate endpoints. Token expiry is the most common failure mode (see README troubleshooting).
- **OpenAI API**: Subject to rate limits and model changes. The classifier uses `temperature=0` (deterministic) but the model itself is a third-party service. All LLM calls have 4-attempt exponential-backoff retry.
- **Slack webhook**: One-way — we POST, we don't read. Slack can return 429 (rate limit) or 5xx (transient). All webhook calls retry up to 4 times.

**What never leaves the boundary:**
- Email body content — sent to OpenAI for classification and drafting, then discarded. Nothing is stored.
- Gmail credentials — `credentials.json` and `token.json` are gitignored and never logged.
