"""
Manual Slack integration test.
Sends a sample digest to your Slack test channel.

Usage:
    python tests/test_slack.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.classifier import ClassifiedEmail, EmailCategory, EmailPriority
from src.draft_generator import DraftResponse, DraftTone
from src.slack_client import send_digest_to_slack


# Build fake enriched emails (mimics what the pipeline produces)
SAMPLE_EMAILS = [
    {
        "subject":      "Budget approval needed for Q4 conference",
        "sender":       "alice.manager@company.com",
        "date":         "2024-11-18",
        "body_preview": "I need your approval on the Q4 conference budget before end of day Friday.",
        "classification": ClassifiedEmail(
            category=EmailCategory.ACTION_NEEDED,
            priority=EmailPriority.HIGH,
            confidence=0.95,
            requires_reply=True,
            one_line_summary="Alice needs budget approval for $3,500 Q4 conference by Friday EOD.",
            suggested_deadline="2024-11-22",
            reasoning="Explicit approval request with a hard deadline.",
        ),
        "draft": DraftResponse(
            subject_line="Re: Budget approval needed for Q4 conference",
            draft_body="Hi Alice,\n\nI've reviewed the Q4 conference budget breakdown and approve the $3,500 allocation. Please proceed with the registration and travel bookings.\n\nBest regards,\n[Your Name]",
            tone_used=DraftTone.FORMAL,
            word_count=38,
            key_points_covered=["Approval granted", "Specific amount confirmed"],
            confidence=0.88,
            human_review_note=None,
        ),
    },
    {
        "subject":      "Weekly team standup notes",
        "sender":       "ops@company.com",
        "date":         "2024-11-18",
        "body_preview": "Notes from today's standup attached. No action items for you.",
        "classification": ClassifiedEmail(
            category=EmailCategory.INTERNAL,
            priority=EmailPriority.LOW,
            confidence=0.92,
            requires_reply=False,
            one_line_summary="Standup notes, FYI only — no action items.",
            suggested_deadline=None,
            reasoning="Routine internal update with no ask.",
        ),
        "draft": None,
    },
]


if __name__ == "__main__":
    print("Sending test Slack digest...")
    success = send_digest_to_slack(
        emails=SAMPLE_EMAILS,
        run_label="Test digest · manual run",
    )
    if success:
        print("Done. Check your Slack channel.")
    else:
        print("Failed. Check logs above.")
        sys.exit(1)
