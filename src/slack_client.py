"""
Slack integration module.

Formats classified emails (with optional drafts) into Slack Block Kit messages
and delivers them via incoming webhook.

Design decisions:
- Uses Block Kit (not plain text) for structured, scannable messages
- Groups emails by priority in a single digest message
- Does NOT include full draft in Slack (too long) — includes first 3 sentences + a note
- Handles webhook failures with retry (webhooks can return 429 or 503 transiently)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import urllib.request
import urllib.error
import structlog

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from config.settings import Settings
from src.classifier import ClassifiedEmail, EmailCategory, EmailPriority
from src.draft_generator import DraftResponse

log = structlog.get_logger(__name__)
_stdlib_log = logging.getLogger(__name__)

settings = Settings()
settings.validate(require=["SLACK_WEBHOOK_URL"])

class _TransientSlackError(Exception):
    """Raised for retryable Slack failures so tenacity can latch onto a single type."""


# ─────────────────────────────────────────────
# Priority → Slack emoji mapping
# ─────────────────────────────────────────────

PRIORITY_EMOJI = {
    EmailPriority.HIGH:   ":red_circle:",
    EmailPriority.MEDIUM: ":large_yellow_circle:",
    EmailPriority.LOW:    ":large_green_circle:",
}

CATEGORY_EMOJI = {
    EmailCategory.ACTION_NEEDED: ":email:",
    EmailCategory.FYI:           ":information_source:",
    EmailCategory.CALENDAR:      ":calendar:",
    EmailCategory.INTERNAL:      ":office:",
    EmailCategory.PERSONAL:      ":person_with_blond_hair:",
    EmailCategory.NEWSLETTER:    ":newspaper:",
    EmailCategory.RECEIPT:       ":receipt:",
    EmailCategory.SPAM:          ":mask:",
}


# ─────────────────────────────────────────────
# Block builders
# ─────────────────────────────────────────────

def _truncate(text: str, max_chars: int = 200) -> str:
    """Slack Block Kit has a 3000 char limit per text block; keep previews short."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "…"


def _draft_preview(draft: DraftResponse, max_sentences: int = 3) -> str:
    """
    Extract the first N sentences from a draft body.
    WHY: full drafts can be 200+ words — too long for a Slack message.
    We show enough to understand the tone + first point, with a note to check email.
    """
    sentences = draft.draft_body.split(". ")
    preview_sentences = sentences[:max_sentences]
    preview = ". ".join(preview_sentences)
    if not preview.endswith("."):
        preview += "…"
    return preview


def _build_email_blocks(
    email: dict,
    classification: ClassifiedEmail,
    draft: Optional[DraftResponse],
) -> list[dict]:
    """
    Build Block Kit blocks for a single email.
    Returns a list of blocks that can be appended to a message's blocks array.
    """
    priority_emoji  = PRIORITY_EMOJI.get(classification.priority, ":white_circle:")
    category_emoji  = CATEGORY_EMOJI.get(classification.category, ":grey_question:")
    subject         = email.get("subject", "(no subject)")
    sender          = email.get("sender", "unknown")
    deadline_text   = f" · Due: *{classification.suggested_deadline}*" if classification.suggested_deadline else ""

    blocks: list[dict] = []

    # Main email row
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"{priority_emoji} {category_emoji} *{subject}*\n"
                f"From: {sender}{deadline_text}\n"
                f"_{classification.one_line_summary}_"
            ),
        },
    })

    # Draft preview (if available)
    if draft is not None:
        preview = _draft_preview(draft)
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":pencil: *Draft:* {preview}",
                }
            ],
        })

    # Confidence + category context
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f"Category: `{classification.category.value}` · "
                    f"Priority: `{classification.priority.value}` · "
                    f"Confidence: {classification.confidence:.0%}"
                ),
            }
        ],
    })

    blocks.append({"type": "divider"})

    return blocks


def build_digest_message(
    emails: list[dict],
    run_label: str = "Email Digest",
    summary: Optional[dict] = None,
) -> dict:
    """
    Build a complete Slack message payload for a batch of classified emails.

    Args:
        emails:    List of enriched email dicts (must have 'classification' and optionally 'draft').
        run_label: Header label for the digest (e.g., "Morning digest · 09:00").

    Returns:
        Slack API payload dict (ready to POST).
    """
    # Sort by priority: high → medium → low
    priority_order = {
        EmailPriority.HIGH: 0,
        EmailPriority.MEDIUM: 1,
        EmailPriority.LOW: 2,
    }

    sorted_emails = sorted(
        emails,
        key=lambda e: priority_order.get(
            e.get("classification", ClassifiedEmail).priority
            if isinstance(e.get("classification"), ClassifiedEmail)
            else EmailPriority.LOW,
            2
        ),
    )

    action_count = sum(
        1 for e in emails
        if isinstance(e.get("classification"), ClassifiedEmail)
        and e["classification"].requires_reply
    )

    if summary is not None:
        count_text = (
            f"*{summary.get('surfaced', 0)} surfaced* · "
            f"{summary.get('filtered', 0)} filtered out · "
            f"{summary.get('fetched', 0)} fetched · "
            f"{action_count} need a reply"
        )
    else:
        count_text = (
            f"*{len(emails)} emails* to review · "
            f"*{action_count}* require a reply"
        )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f":inbox_tray: {run_label}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": count_text}},
        {"type": "divider"},
    ]

    if not emails:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":tada: Inbox zero. No emails to review."},
        })
        return {"blocks": blocks}

    for email in sorted_emails:
        classification: Optional[ClassifiedEmail] = email.get("classification")
        draft: Optional[DraftResponse]            = email.get("draft")

        if classification is None:
            log.warning("digest.email_missing_classification", subject=email.get("subject"))
            continue

        email_blocks = _build_email_blocks(email, classification, draft)
        blocks.extend(email_blocks)

    # Footer
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"Generated by ai-email-agent · Drafts are suggestions, review before sending"}
        ],
    })

    return {"blocks": blocks}


# ─────────────────────────────────────────────
# Webhook sender
# ─────────────────────────────────────────────

class SlackWebhookError(Exception):
    """Raised when the Slack webhook returns a non-200 response."""
    pass


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=16),
    retry=retry_if_exception_type(_TransientSlackError),
    before_sleep=before_sleep_log(_stdlib_log, logging.WARNING),
    reraise=True,
)
def _post_to_webhook(payload: dict, webhook_url: str) -> None:
    """
    POST a payload to a Slack webhook URL.
    WHY urllib instead of requests: avoids adding a dependency just for one POST call.
    In a larger project, use httpx or requests — but for a single webhook call, stdlib is fine.
    """
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                raise _TransientSlackError(f"unexpected status {resp.status}")

    except urllib.error.HTTPError as e:
        if e.code == 429 or 500 <= e.code < 600:
            # 429 = rate limit, transient. 5xx = server problem, transient.
            raise _TransientSlackError(f"HTTP {e.code}") from e
        log.error("slack.post.bad_request", status=e.code, body=e.read()[:500])
        raise 

    except urllib.error.URLError as e:
        # DNS failure, connection refused, timeout — almost always transient.
        raise _TransientSlackError(str(e)) from e


def send_digest_to_slack(
    emails: list[dict],
    run_label: str = "Email Digest",
    webhook_url: Optional[str] = None,
    summary: Optional[dict] = None,   # ← new
) -> bool:

    """
    Build and send a digest message to Slack.

    Args:
        emails:      List of enriched email dicts.
        run_label:   Header for the message.
        webhook_url: Override webhook URL (defaults to SLACK_WEBHOOK_URL env var).

    Returns:
        True on success, False on failure (logs the error).
    """
    url = webhook_url or settings.SLACK_WEBHOOK_URL

    payload = build_digest_message(emails, run_label=run_label, summary=summary)

    log.debug("slack.payload.prepared", block_count=len(payload.get("blocks", [])))

    try:
        _post_to_webhook(payload, url)
        return True
    except SlackWebhookError:
        log.exception("slack.digest.failed_after_retries")
        return False
