"""
Draft response generator.

Takes a ClassifiedEmail + the original email content and produces
a ready-to-send (or lightly edited) draft reply.

Design decisions:
- Only generates drafts for emails where requires_reply=True
- Supports formal/casual/brief tone via parameter
- Returns structured output (not raw text) so downstream code can inspect metadata
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
from src.classifier import ClassifiedEmail, EmailCategory

logger = logging.getLogger(__name__)

settings = Settings()
settings.validate(require=["OPENAI_API_KEY"])
client = OpenAI(api_key=settings.OPENAI_API_KEY)


# ─────────────────────────────────────────────
# Domain model
# ─────────────────────────────────────────────

class DraftTone(str, Enum):
    FORMAL  = "formal"   # Professional, third-person, full sentences
    CASUAL  = "casual"   # First-person, contractions, friendly
    BRIEF   = "brief"    # 2–3 sentences max, no pleasantries


class DraftResponse(BaseModel):
    """
    Structured output from the draft generator.
    """
    subject_line:       str   = Field(description="Email subject line (usually Re: original)")
    draft_body:         str   = Field(description="Complete email body ready to send or lightly edit")
    tone_used:          DraftTone
    word_count:         int   = Field(ge=1)
    key_points_covered: list[str] = Field(description="Bullet list of what the draft addresses")
    confidence:         float = Field(ge=0.0, le=1.0)
    human_review_note:  Optional[str] = Field(
        default=None,
        description="If the draft makes assumptions or needs human verification, note it here"
    )


# ─────────────────────────────────────────────
# Tone-aware system prompts
# ─────────────────────────────────────────────

TONE_INSTRUCTIONS = {
    DraftTone.FORMAL: """
Write in a professional, formal tone.
- Use complete sentences and proper punctuation.
- Open with "Dear [Name]" or "Hello [Name],".
- Close with "Best regards," or "Kind regards,".
- Do not use contractions (write "I would" not "I'd").
- Keep a business-appropriate distance.
- A formal tone does not mean a decisive tone — if a decision is required, [DECISION NEEDED] is more professional than guessing on the human's behalf.
""",
    DraftTone.CASUAL: """
Write in a friendly, casual tone as if to a colleague you know well.
- Use contractions freely (I'd, can't, we'll).
- Open with "Hi [Name]," or just their name.
- Close with "Thanks," or "Cheers,".
- Keep it warm and conversational.
""",
    DraftTone.BRIEF: """
Write an extremely concise reply — maximum 3 sentences.
- No pleasantries, no padding.
- Answer the ask directly in the fewest words possible.
- No greeting or sign-off unless the context demands it.
""",
}

BASE_SYSTEM_PROMPT = """You are a senior professional helping draft email replies.

Your principles:
1. Answer the specific ask — read what's actually being asked before writing anything.
2. Never hallucinate facts — if you don't know something (dates, prices, names), leave a [PLACEHOLDER] so the human can fill it in.
3. Never make decisions on behalf of the human. If the email requires a decision (approve/decline, agree/disagree, accept/reject), write [DECISION NEEDED: describe the choice] and let the human fill it in. Do not pick a side.
4. Be direct — get to the point immediately. But if the point requires a decision you don't have, lead with [DECISION NEEDED] in the first sentence instead.
5. Never add unnecessary throat-clearing ("Thanks for reaching out", "I hope this email finds you well").
6. Match the original email's language — if they write in Spanish, reply in Spanish.
{tone_instructions}

Output a complete, ready-to-send email draft.
"""


def _build_system_prompt(tone: DraftTone) -> str:
    return BASE_SYSTEM_PROMPT.format(tone_instructions=TONE_INSTRUCTIONS[tone])


def _build_user_prompt(
    subject: str,
    sender: str,
    body: str,
    classification: ClassifiedEmail,
) -> str:
    return f"""Draft a reply to this email.

--- ORIGINAL EMAIL ---
From: {sender}
Subject: {subject}

{body[:2000]}
--- END ---

Context from classifier:
- One-line summary: {classification.one_line_summary}
- Deadline: {classification.suggested_deadline or 'none mentioned'}
- Key ask: this is classified as {classification.category.value} / {classification.priority.value} priority

Write a complete reply draft. If this email requires a decision (approve/decline, 
agree/disagree, accept/reject), write [DECISION NEEDED: describe the choice] 
instead of deciding. Never choose on behalf of the human..
"""


# ─────────────────────────────────────────────
# Core function
# ─────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type((RateLimitError, APIConnectionError, InternalServerError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def generate_draft(
    subject: str,
    sender: str,
    body: str,
    classification: ClassifiedEmail,
    tone: DraftTone = DraftTone.FORMAL,
    model: str = "gpt-4o-mini",
) -> Optional[DraftResponse]:
    """
    Generate a draft reply for an email.

    Returns None if the email does not require a reply (skipped gracefully).
    Returns DraftResponse with the draft body and metadata otherwise.

    Args:
        subject:        Email subject line.
        sender:         Sender address / display name.
        body:           Full email body (or preview).
        classification: ClassifiedEmail from classifier.py.
        tone:           DraftTone enum — formal, casual, or brief.
        model:          OpenAI model string.
    """
    # Guard: only draft for emails that need a reply
    if not classification.requires_reply:
        logger.debug(
            "Skipping draft generation — requires_reply=False for subject=%r category=%s",
            subject,
            classification.category.value,
        )
        return None

    logger.debug("Generating draft for subject=%r tone=%s", subject, tone.value)

    completion = client.beta.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": _build_system_prompt(tone)},
            {"role": "user",   "content": _build_user_prompt(subject, sender, body, classification)},
        ],
        response_format=DraftResponse,
        temperature=0.3,    # WHY 0.3: slightly creative but consistent — drafts should sound human
        max_tokens=800,
    )

    result: DraftResponse = completion.choices[0].message.parsed

    logger.info(
        "Draft generated | subject=%r tone=%s words=%d confidence=%.2f",
        subject,
        result.tone_used.value,
        result.word_count,
        result.confidence,
    )

    if result.human_review_note:
        logger.warning("Draft needs human review: %s", result.human_review_note)

    return result


def generate_drafts_for_batch(
    emails: list[dict],
    tone: DraftTone = DraftTone.FORMAL,
) -> list[dict]:
    """
    Generate drafts for a batch of classified emails.
    Attaches a `draft` key to each email dict (None if no draft needed).

    Args:
        emails: List of dicts, each with a `classification` key (from classify_emails_batch).
        tone:   Tone to use for all drafts.

    Returns:
        Same list with `draft` key added to each dict.
    """
    results: list[dict] = []

    for email in emails:
        classification: ClassifiedEmail = email.get("classification")

        if classification is None:
            logger.warning("Email missing classification key — skipping draft: %r", email.get("subject"))
            email["draft"] = None
            results.append(email)
            continue

        try:
            draft = generate_draft(
                subject=email.get("subject", ""),
                sender=email.get("sender", ""),
                body=email.get("body_preview", email.get("snippet", "")),
                classification=classification,
                tone=tone,
            )
            email["draft"] = draft

        except Exception:
            logger.exception("Failed to generate draft for subject=%r — skipping", email.get("subject"))
            email["draft"] = None

        results.append(email)

    generated_count = sum(1 for e in results if e.get("draft") is not None)
    logger.info("Generated %d drafts for %d emails", generated_count, len(results))
    return results
