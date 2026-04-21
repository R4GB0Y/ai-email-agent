"""
Email classifier module.

Classifies raw email text into a fixed taxonomy using structured LLM output.
Follows the same pattern as agent.py: Pydantic model → OpenAI structured output → retry.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from openai import OpenAI, RateLimitError, APIConnectionError, InternalServerError
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from config.settings import Settings

logger = logging.getLogger(__name__)

settings = Settings()
settings.validate(require=["OPENAI_API_KEY"])
client = OpenAI(api_key=settings.OPENAI_API_KEY)


# ─────────────────────────────────────────────
# Domain model
# ─────────────────────────────────────────────

class EmailCategory(str, Enum):
    """
    Fixed taxonomy of email categories.
    WHY str + Enum: JSON serialization works out of the box (the value IS the string).
    Add new categories here, nowhere else — single source of truth.
    """
    ACTION_NEEDED   = "action_needed"    # Requires a reply or task from you
    FYI             = "fyi"              # Read-only update, no reply needed
    NEWSLETTER      = "newsletter"       # Subscriptions, digests, product updates
    RECEIPT         = "receipt"          # Order confirmations, invoices, payments
    CALENDAR        = "calendar"         # Meeting invites, scheduling requests
    SPAM            = "spam"             # Unsolicited, irrelevant
    PERSONAL        = "personal"         # From family / friends
    INTERNAL        = "internal"         # From your own team / org


class EmailPriority(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"


class ClassifiedEmail(BaseModel):
    """
    Structured output returned by the classifier.
    This is the contract between the classifier and every downstream stage.
    WHY Pydantic: validates at construction time, serializes to JSON, self-documents.
    """
    category:           EmailCategory
    priority:           EmailPriority
    confidence:         float = Field(ge=0.0, le=1.0, description="0–1 confidence score")
    requires_reply:     bool  = Field(description="True if a human response is expected")
    one_line_summary:   str   = Field(max_length=120, description="Single sentence capturing the ask or point")
    suggested_deadline: Optional[str] = Field(
        default=None,
        description="ISO 8601 date string if the email has an explicit deadline, else null"
    )
    reasoning:          str   = Field(description="1–2 sentences explaining this classification")


# ─────────────────────────────────────────────
# Prompt
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert email triage assistant working for a busy professional.

Your job: classify incoming emails accurately and concisely.

Classification rules:
- action_needed: The sender explicitly or implicitly expects a response or a task to be completed.
  Examples: questions directed at you, approval requests, meeting scheduling requests, client asks.
- fyi: Informational only. CC'd emails, status updates, announcements where no response is needed.
- newsletter: Marketing, product updates, digests, subscription content.
- receipt: Transaction confirmations, invoices, shipping notifications, bank statements.
- calendar: Calendar invitations, meeting requests, scheduling polls (Calendly, Doodle).
- spam: Unsolicited, clearly irrelevant or suspicious emails.
- personal: From family, friends, personal contacts unrelated to work.
- internal: From colleagues or teammates; workplace communication.

Priority rules:
- high: Deadline within 24 hours, escalation from a senior person, client-facing urgency.
- medium: Response needed within a week; normal business communication.
- low: No urgency; newsletters, FYIs, receipts are almost always low.

Be conservative with confidence: if the subject and body give mixed signals, score 0.6–0.75.
Always provide a one_line_summary even for spam or newsletters (summarize what it is).
"""


def _build_user_prompt(subject: str, sender: str, body_preview: str) -> str:
    return f"""Classify this email:

Subject: {subject}
From: {sender}
Body preview:
{body_preview[:1500]}
"""


# ─────────────────────────────────────────────
# Core function with retry
# ─────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type((RateLimitError, APIConnectionError, InternalServerError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def classify_email(
    subject: str,
    sender: str,
    body_preview: str,
    model: str = "gpt-4o-mini",
) -> ClassifiedEmail:
    """
    Classify a single email using structured LLM output.

    Args:
        subject:      Email subject line.
        sender:       Sender address / display name.
        body_preview: First ~1500 chars of the email body.
        model:        OpenAI model string.

    Returns:
        ClassifiedEmail with guaranteed schema.

    Raises:
        openai.* errors after 4 retries.
        pydantic.ValidationError if the model returns an unexpected schema (very rare with gpt-4o-mini).
    """
    logger.debug("Classifying email: subject=%r sender=%r", subject, sender)

    completion = client.beta.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": _build_user_prompt(subject, sender, body_preview)},
        ],
        response_format=ClassifiedEmail,
        temperature=0,      # WHY 0: classifiers should be deterministic — no creativity needed
        max_tokens=400,
    )

    result: ClassifiedEmail = completion.choices[0].message.parsed

    logger.info(
        "Classified email | subject=%r category=%s priority=%s confidence=%.2f requires_reply=%s",
        subject,
        result.category.value,
        result.priority.value,
        result.confidence,
        result.requires_reply,
    )

    return result


def classify_emails_batch(
    emails: list[dict],
    skip_categories: set[str] | None = None,
) -> list[dict]:
    """
    Classify a list of email dicts (as returned by gmail_client.py).
    Attaches a `classification` key to each dict.
    Skips emails whose category ends up in skip_categories (useful for filtering spam/newsletters).

    Args:
        emails:          List of email dicts from GmailClient.fetch_unread_emails().
        skip_categories: Set of EmailCategory values to exclude from output.
                         Defaults to {"spam", "newsletter", "receipt"}.

    Returns:
        Filtered list of email dicts, each with a `classification` key containing ClassifiedEmail.
    """
    if skip_categories is None:
        skip_categories = {"spam", "newsletter", "receipt"}

    results: list[dict] = []

    for email in emails:
        try:
            classification = classify_email(
                subject=email.get("subject", "(no subject)"),
                sender=email.get("sender", "unknown"),
                body_preview=email.get("body_preview", email.get("snippet", "")),
            )

            if classification.category.value in skip_categories:
                logger.debug("Skipping email (category=%s): %r", classification.category.value, email.get("subject"))
                continue

            enriched = {**email, "classification": classification}
            results.append(enriched)

        except Exception:
            logger.exception("Failed to classify email subject=%r — skipping", email.get("subject"))
            continue

    logger.info("Classified %d/%d emails (after filtering)", len(results), len(emails))
    return results
