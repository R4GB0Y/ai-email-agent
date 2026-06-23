"""
Full email agent pipeline.

Orchestrates: Gmail fetch → classify → draft → Slack digest.
Runs on a schedule via APScheduler (every 6 hours by default).

Usage:
    # Run once immediately:
    python -m src.pipeline --run-once

    # Run on schedule (every 6 hours):
    python -m src.pipeline

    # Run with custom interval (for testing):
    python -m src.pipeline --interval-hours 0.1
"""

from __future__ import annotations

import argparse
#import logging  
import sys
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import Settings
from src.gmail_client import fetch_unread_emails
from src.classifier import classify_emails_batch
from src.draft_generator import generate_drafts_for_batch, DraftTone
from src.slack_client import send_digest_to_slack
import structlog
from src.logging_config import configure_logging
import uuid
import time
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os

# ─────────────────────────────────────────────
# Logging setup
# WHY basicConfig at pipeline level: the pipeline is the entry point,
# so it owns the logging configuration. Library modules just call log = structlog.get_logger(__name__). Using the structlog logger is better because it's more flexible and can be used in a more consistent way across the codebase.
# ─────────────────────────────────────────────


#        logging.basicConfig(
 #           level=logging.INFO,
 #           format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
 #           datefmt="%Y-%m-%d %H:%M:%S",
 #           handlers=[logging.StreamHandler(sys.stdout)],
 #       )
configure_logging()
log = structlog.get_logger(__name__)
settings = Settings()
settings.validate(require=["OPENAI_API_KEY", "SLACK_WEBHOOK_URL"])


# ─────────────────────────────────────────────
# Pipeline configuration
# WHY dataclass-style config at the top: makes it easy to tune without
# hunting through code. One place to change behaviour.
# ─────────────────────────────────────────────

SKIP_CATEGORIES    = {"spam", "newsletter", "receipt"}  # Not worth surfacing
DRAFT_TONE         = DraftTone.FORMAL
MAX_EMAILS_PER_RUN = 20   # Safety cap — prevents a flooded inbox from sending 100 Slack blocks
SCHEDULE_HOURS     = 6    # How often the pipeline runs



def run_pipeline(run_label: Optional[str] = None) -> dict:
    """
    Execute one full pipeline run.

    Returns a summary dict with counts for observability.
    This is pure Python — no scheduler knowledge needed here.

    Args:
        run_label: Optional label for the Slack message header.
                   Defaults to "Email digest · HH:MM".
    """
    start_time = datetime.now()
    label      = run_label or f"Email digest · {start_time.strftime('%H:%M')}"

    # NEW: 3 lines for the binding run context in order to be able to track the 
    run_id = uuid.uuid4().hex[:8]
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id=run_id)


    log.info("pipeline.started", label=label)

    summary = {
        "fetched": 0,
        "processed": 0,   # emails the LLM classified
        "surfaced": 0,    # emails that passed the priority filter
        "filtered": 0,    # processed - surfaced, for readability
        "drafted": 0,
        "sent": False,
        "errors": [],
    }
    surfaced: list = []
    classified_emails: list = []
    enriched_emails: list = []

    try:
        # ── Stage 1: Fetch emails ───────────────────────────────────────
        log.info("stage.fetch.starting", max_results=MAX_EMAILS_PER_RUN)
        try:
            emails = fetch_unread_emails(max_results=MAX_EMAILS_PER_RUN)
            summary["fetched"] = len(emails)
            log.info("stage.fetch.ok", count=len(emails))
        except Exception as e:
            log.exception("stage.fetch.failed")
            summary["errors"].append(f"Gmail fetch: {e}")
            return summary  # Can't proceed without emails

        if not emails:
            log.info("pipeline.no_work", reason="no_unread_emails")
            send_digest_to_slack([], run_label=label, summary=summary)
            summary["sent"] = True
            return summary

        # ── Stage 2: Classify ───────────────────────────────────────────
        log.info("stage.classify.starting", skip_categories=SKIP_CATEGORIES)
        try:
            classified_emails = classify_emails_batch(emails, skip_categories=SKIP_CATEGORIES)
            summary["processed"] = len(classified_emails)   # everything the LLM processed 
            log.info("stage.classify.ok", processed=len(classified_emails))
            surfaced = [e for e in classified_emails if e["classification"].priority != "low"]
            summary["surfaced"] = len(surfaced)
            summary["filtered"] = summary["processed"] - summary["surfaced"] # for readability
            log.info("stage.filter.ok", surfaced=summary["surfaced"], filtered=summary["filtered"])

        except Exception as e:
            log.exception("stage.classify.failed")
            summary["errors"].append(f"Classification: {e}")
            classified_emails = []  # Degrade gracefully: send Slack with raw email info

            
        # ── Stage 3: Generate drafts ────────────────────────────────────
        log.info("stage.draft.starting", tone=DRAFT_TONE.value, candidate_count=len(surfaced))
        try:
            enriched_emails = generate_drafts_for_batch(surfaced, tone=DRAFT_TONE)
            summary["drafted"] = sum(1 for e in enriched_emails if e.get("draft") is not None)
            log.info("stage.draft.ok", drafted=summary["drafted"])
        except Exception as e:
            log.exception("stage.draft.failed")
            summary["errors"].append(f"Draft generation: {e}")
            enriched_emails = [{"classification": c, "draft": None, **getattr(c, "__dict__", {})} for c in surfaced]  # Degrade: send without drafts

        # ── Stage 4: Send to Slack ──────────────────────────────────────
        log.info("stage.slack.starting", email_count=len(enriched_emails))
        try:
            success = send_digest_to_slack(enriched_emails, run_label=label, summary=summary)
            summary["sent"] = success
            if success:
                log.info("stage.slack.ok")
            else:
                log.error("stage.slack.failed", reason="non_raising_false_return")
                summary["errors"].append("Slack send returned False")
        except Exception as e:
            log.exception("stage.slack.failed")
            summary["errors"].append(f"Slack: {e}")

        # ── Summary ─────────────────────────────────────────────────────
        elapsed = (datetime.now() - start_time).total_seconds()
        log.info(
            "pipeline.finished",
            elapsed_seconds=round(elapsed, 2),
            fetched=summary["fetched"],
            processed=summary["processed"],
            surfaced=summary["surfaced"],
            filtered=summary["filtered"],
            drafted=summary["drafted"],
            sent=summary["sent"],
            error_count=len(summary["errors"]),
            )
        if summary["errors"]:
            log.warning("pipeline.errors", error_count=len(summary["errors"]))

        return summary

    finally:
        # For the clean up
        log.info("pipeline.cleanup")
        structlog.contextvars.clear_contextvars()


# ─────────────────────────────────────────────
# Health endpoint
# Background HTTP server reports the status of the last pipeline run.
# Lives in a daemon thread alongside the scheduler.
# ─────────────────────────────────────────────

# Module-level state — fine here because it's single-process, single-writer.
_last_run: dict = {"status": "never_run", "summary": None, "finished_at": None}
_last_run_lock = threading.Lock()


def _record_run(summary: dict, status: str) -> None:
    with _last_run_lock:
        _last_run["status"] = status
        _last_run["summary"] = summary
        _last_run["finished_at"] = time.time()


def _scheduled_job() -> None:
    try:
        summary = run_pipeline()
        status = "ok" if summary.get("sent") else "degraded"
        _record_run(summary, status)
    except Exception:
        log.exception("pipeline.crashed")
        _record_run({}, "crashed")


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/health", "/"):
            self.send_response(404)
            self.end_headers()
            return

        with _last_run_lock:
            snapshot = dict(_last_run)

        # Liveness: the process is up. Readiness: we've had at least one successful run.
        healthy = snapshot["status"] in ("ok", "never_run")
        code = 200 if healthy else 503
        body = json.dumps({
            "healthy": healthy,
            "last_run_status": snapshot["status"],
            "last_run_finished_at": snapshot["finished_at"],
            "last_run_summary": snapshot["summary"],
        }, default=str).encode()

        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # silence default stderr access log
        return


def _start_health_server(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health")
    thread.start()

# ─────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────

def start_scheduler(interval_hours: float = SCHEDULE_HOURS) -> None:
    """
    Start APScheduler and run the pipeline on a fixed interval.

    WHY BlockingScheduler: it blocks the main thread, keeping the process alive.
    IntervalTrigger: runs immediately on start, then every `interval_hours`.
    """
    # 1. Start health server first
    port = int(os.getenv("PORT", "8080"))
    _start_health_server(port)
    log.info("health_server.started", port=port)

    # 2. Start scheduler
    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        func=_scheduled_job,
        trigger=IntervalTrigger(hours=interval_hours),
        id="email_pipeline",
        name="Email agent pipeline",
        replace_existing=True,
        max_instances=1,        # WHY: prevents overlapping runs if one takes longer than the interval
        misfire_grace_time=300, # WHY: if a run is missed (sleep, restart), allow 5 min catchup
    )

    log.info("scheduler.starting", interval_hours=interval_hours)

    try:
        # Run immediately before handing off to scheduler
        log.info("scheduler.run_immediate")
        _scheduled_job()

        scheduler.start()

    except KeyboardInterrupt:

        log.info("scheduler.stopped", reason="keyboard_interrupt")
        scheduler.shutdown(wait=False)



# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Email agent pipeline")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run the pipeline once and exit (no scheduler).",
    )
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=SCHEDULE_HOURS,
        help=f"Schedule interval in hours (default: {SCHEDULE_HOURS}). Use 0.1 for 6-minute testing.",
    )
    args = parser.parse_args()

    if args.run_once:
        summary = run_pipeline()
        sys.exit(0 if not summary["errors"] else 1)
    else:
        start_scheduler(interval_hours=args.interval_hours)


if __name__ == "__main__":
    main()
