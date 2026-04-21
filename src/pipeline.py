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
import logging
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

# ─────────────────────────────────────────────
# Logging setup
# WHY basicConfig at pipeline level: the pipeline is the entry point,
# so it owns the logging configuration. Library modules just call logger = logging.getLogger(__name__).
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

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

    logger.info("=" * 60)
    logger.info("Pipeline run starting: %s", label)
    logger.info("=" * 60)

    summary = {
        "fetched": 0,
        "classified": 0,
        "drafted": 0,
        "slack_sent": False,
        "errors": [],
    }

    # ── Stage 1: Fetch emails ───────────────────────────────────────
    logger.info("[1/4] Fetching unread emails from Gmail...")
    try:
        emails = fetch_unread_emails(max_results=MAX_EMAILS_PER_RUN)
        summary["fetched"] = len(emails)
        logger.info("      Fetched %d emails", len(emails))
    except Exception as e:
        logger.exception("Gmail fetch failed — aborting pipeline run")
        summary["errors"].append(f"Gmail fetch: {e}")
        return summary  # Can't proceed without emails

    if not emails:
        logger.info("No unread emails. Sending 'inbox zero' message to Slack.")
        send_digest_to_slack([], run_label=label)
        summary["slack_sent"] = True
        return summary

    # ── Stage 2: Classify ───────────────────────────────────────────
    logger.info("[2/4] Classifying emails (skipping: %s)...", ", ".join(SKIP_CATEGORIES))
    try:
        classified_emails = classify_emails_batch(emails, skip_categories=SKIP_CATEGORIES)
        summary["classified"] = len(classified_emails)
        logger.info("      %d emails remain after filtering", len(classified_emails))
    except Exception as e:
        logger.exception("Classification stage failed")
        summary["errors"].append(f"Classification: {e}")
        classified_emails = []  # Degrade gracefully: send Slack with raw email info

    # ── Stage 3: Generate drafts ────────────────────────────────────
    logger.info("[3/4] Generating drafts for action-needed emails...")
    try:
        enriched_emails = generate_drafts_for_batch(classified_emails, tone=DRAFT_TONE)
        summary["drafted"] = sum(1 for e in enriched_emails if e.get("draft") is not None)
        logger.info("      Generated %d drafts", summary["drafted"])
    except Exception as e:
        logger.exception("Draft generation stage failed — will send without drafts")
        summary["errors"].append(f"Draft generation: {e}")
        enriched_emails = classified_emails  # Degrade: send without drafts

    # ── Stage 4: Send to Slack ──────────────────────────────────────
    logger.info("[4/4] Sending digest to Slack...")
    try:
        success = send_digest_to_slack(enriched_emails, run_label=label)
        summary["slack_sent"] = success
        if success:
            logger.info("      Slack digest sent successfully")
        else:
            logger.error("      Slack send failed (non-raising)")
            summary["errors"].append("Slack send returned False")
    except Exception as e:
        logger.exception("Slack send failed")
        summary["errors"].append(f"Slack: {e}")

    # ── Summary ─────────────────────────────────────────────────────
    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("=" * 60)
    logger.info(
        "Pipeline run complete in %.1fs | fetched=%d classified=%d drafted=%d slack=%s errors=%d",
        elapsed,
        summary["fetched"],
        summary["classified"],
        summary["drafted"],
        summary["slack_sent"],
        len(summary["errors"]),
    )
    if summary["errors"]:
        logger.warning("Errors: %s", summary["errors"])
    logger.info("=" * 60)

    return summary


# ─────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────

def start_scheduler(interval_hours: float = SCHEDULE_HOURS) -> None:
    """
    Start APScheduler and run the pipeline on a fixed interval.

    WHY BlockingScheduler: it blocks the main thread, keeping the process alive.
    IntervalTrigger: runs immediately on start, then every `interval_hours`.
    """
    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        func=run_pipeline,
        trigger=IntervalTrigger(hours=interval_hours),
        id="email_pipeline",
        name="Email agent pipeline",
        replace_existing=True,
        max_instances=1,        # WHY: prevents overlapping runs if one takes longer than the interval
        misfire_grace_time=300, # WHY: if a run is missed (sleep, restart), allow 5 min catchup
    )

    logger.info(
        "Scheduler starting. Pipeline will run every %.1f hour(s). Press Ctrl+C to stop.",
        interval_hours,
    )

    try:
        # Run immediately before handing off to scheduler
        logger.info("Running pipeline immediately on startup...")
        run_pipeline()

        scheduler.start()

    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user.")
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
