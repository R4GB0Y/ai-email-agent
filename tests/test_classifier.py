"""
Classifier accuracy test.

Usage:
    python -m pytest tests/test_classifier.py -v

    # Or run the accuracy report directly:
    python tests/test_classifier.py

WHY a separate accuracy test vs unit test:
    Unit tests mock the LLM. Accuracy tests hit the real API with real labeled examples.
    Keep them separate — unit tests run in CI (fast, free), accuracy tests run manually (slow, costs money).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.classifier import classify_email, EmailCategory

# ─────────────────────────────────────────────
# Labeled test set — replace / extend with YOUR real emails
# Format: (subject, sender, body_preview, expected_category)
# ─────────────────────────────────────────────

TEST_CASES: list[tuple[str, str, str, str]] = [
    # action_needed
    (
        "Quick question about the Q3 roadmap",
        "sarah@company.com",
        "Hi, I was reviewing the roadmap doc you shared and had a question about the prioritization of the payments feature. Can you jump on a quick call this week to discuss? Thanks, Sarah",
        "action_needed",
    ),
    (
        "Can you approve this PR when you get a chance?",
        "dev@company.com",
        "Hey, I've opened PR #234 for the new auth flow. It's been sitting for two days — can you take a look and approve when you have a moment? Link: github.com/...",
        "action_needed",
    ),
    (
        "Invoice overdue — please respond",
        "billing@vendor.com",
        "Your invoice #INV-9921 for $1,200 was due on the 15th. Please arrange payment within 48 hours to avoid service interruption.",
        "action_needed",
    ),

    # fyi
    (
        "New hire started today — welcome Alex",
        "hr@company.com",
        "Just a heads up that Alex joined the data team today. Please give them a warm welcome! They'll be sitting next to the coffee machine.",
        "fyi",
    ),
    (
        "Deployment completed successfully",
        "ci@github.com",
        "Your deployment to production completed at 14:32 UTC. All health checks passed. No action required.",
        "fyi",
    ),

    # newsletter
    (
        "The Weekly Digest: Top AI papers this week",
        "digest@aiweekly.com",
        "This week in AI: GPT-5 rumours, Mistral's new model, and a breakthrough in protein folding. Click to read more...",
        "newsletter",
    ),
    (
        "Your monthly product update from Notion",
        "noreply@notion.so",
        "New in Notion this month: AI autofill for databases, improved mobile editor, and dark mode improvements.",
        "newsletter",
    ),

    # receipt
    (
        "Your Amazon order has shipped",
        "shipment-tracking@amazon.com",
        "Your order #112-3456789 containing 'Python Crash Course' has shipped. Estimated delivery: Thursday. Track your package: amazon.com/...",
        "receipt",
    ),
    (
        "Receipt from Stripe — $29.00",
        "receipts@stripe.com",
        "You were charged $29.00 on your Visa ending in 4242 for your monthly subscription to Notion. Invoice attached.",
        "receipt",
    ),

    # calendar
    (
        "Invitation: Weekly team standup @ Tue 9am",
        "calendar-notification@google.com",
        "You have been invited to: Weekly team standup. Tuesday 9:00am - 9:15am. Organizer: manager@company.com. RSVP to accept or decline.",
        "calendar",
    ),

    # spam
    (
        "CONGRATULATIONS! You've been selected",
        "noreply@prize-winner2024.com",
        "Dear valued customer, you have been randomly selected to receive a $500 Amazon gift card. Click here to claim your prize now! Limited time offer.",
        "spam",
    ),

    # internal
    (
        "Reminder: all-hands tomorrow at 3pm",
        "ops@yourcompany.com",
        "Don't forget — all-hands meeting tomorrow at 3pm in Conference Room B. Agenda: Q3 results, roadmap preview, open Q&A.",
        "internal",
    ),

    # personal
    (
        "Dinner Saturday?",
        "mom@gmail.com",
        "Hey, are you free Saturday evening? Dad and I thought we could do dinner at that Italian place you like. Let us know!",
        "personal",
    ),
]


def run_accuracy_test(verbose: bool = True) -> float:
    """
    Run all labeled test cases and report accuracy.
    Returns accuracy as a float (0.0–1.0).
    """
    correct = 0
    total   = len(TEST_CASES)
    failures: list[str] = []

    for i, (subject, sender, body, expected) in enumerate(TEST_CASES):
        try:
            result = classify_email(subject=subject, sender=sender, body_preview=body)
            actual = result.category.value

            if actual == expected:
                correct += 1
                if verbose:
                    print(f"  ✓ [{i+1:02d}] {subject[:50]:<50} → {actual}")
            else:
                failures.append(
                    f"  ✗ [{i+1:02d}] {subject[:50]:<50}\n"
                    f"          expected={expected:<20} got={actual}\n"
                    f"          reasoning: {result.reasoning}"
                )
                if verbose:
                    print(failures[-1])

        except Exception as e:
            failures.append(f"  ✗ [{i+1:02d}] {subject[:50]:<50} — ERROR: {e}")
            if verbose:
                print(failures[-1])

    accuracy = correct / total
    print(f"\n{'─'*60}")
    print(f"  Accuracy: {correct}/{total} = {accuracy:.1%}")

    if accuracy >= 0.85:
        print(f"  Status: PASS (≥85% target)")
    else:
        print(f"  Status: FAIL (<85% target)")
        print(f"\n  Failed cases:")
        for f in failures:
            print(f)

    return accuracy


# pytest-compatible tests
def test_action_needed_classification():
    result = classify_email(
        subject="Quick question about the Q3 roadmap",
        sender="sarah@company.com",
        body_preview="Can you jump on a quick call this week to discuss prioritization?",
    )
    assert result.category == EmailCategory.ACTION_NEEDED
    assert result.requires_reply is True


def test_newsletter_classification():
    result = classify_email(
        subject="The Weekly Digest: Top AI papers this week",
        sender="digest@aiweekly.com",
        body_preview="This week in AI: GPT-5 rumours, Mistral's new model...",
    )
    assert result.category == EmailCategory.NEWSLETTER
    assert result.requires_reply is False


def test_spam_is_low_priority():
    result = classify_email(
        subject="CONGRATULATIONS! You've been selected",
        sender="noreply@prize-winner2024.com",
        body_preview="Click here to claim your prize now!",
    )
    assert result.category == EmailCategory.SPAM


if __name__ == "__main__":
    print("Running email classifier accuracy test...\n")
    accuracy = run_accuracy_test(verbose=True)
    sys.exit(0 if accuracy >= 0.85 else 1)
