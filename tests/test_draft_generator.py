"""
Manual test / demo for draft generator.

Usage:
    python tests/test_draft_generator.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.classifier import classify_email
from src.draft_generator import generate_draft, DraftTone

TEST_EMAIL = {
    "subject": "Budget approval needed for Q4 conference",
    "sender":  "alice.manager@company.com",
    "body":    """Hi,

I need your approval on the Q4 conference budget before end of day Friday.
The total is $3,500 covering registration ($1,200), flights ($1,500), and hotel ($800).

Let me know if you have questions or need any adjustments.

Best,
Alice""",
}


def test_all_tones():
    print("Classifying email...")
    classification = classify_email(
        subject=TEST_EMAIL["subject"],
        sender=TEST_EMAIL["sender"],
        body_preview=TEST_EMAIL["body"],
    )
    print(f"  Category: {classification.category.value}")
    print(f"  Summary:  {classification.one_line_summary}")
    print(f"  Requires reply: {classification.requires_reply}\n")

    for tone in DraftTone:
        print(f"{'─'*60}")
        print(f"Tone: {tone.value.upper()}")
        print('─'*60)
        draft = generate_draft(
            subject=TEST_EMAIL["subject"],
            sender=TEST_EMAIL["sender"],
            body=TEST_EMAIL["body"],
            classification=classification,
            tone=tone,
        )
        if draft:
            print(f"Subject: {draft.subject_line}")
            print(f"\n{draft.draft_body}")
            print(f"\n[{draft.word_count} words | confidence: {draft.confidence:.0%}]")
            if draft.human_review_note:
                print(f"[Review note: {draft.human_review_note}]")
        print()


if __name__ == "__main__":
    test_all_tones()
