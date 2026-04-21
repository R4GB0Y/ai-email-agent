"""
Gmail API client — fetch and parse unread emails.

KEY CONCEPTS:
1. OAuth2 — The user grants YOUR app permission to read THEIR email.
   You never see their password. Google gives you a token instead.
2. Token refresh — Tokens expire. We save them to avoid re-auth every time.
3. MIME parsing — Emails aren't plain text. They're nested containers
   (like Russian dolls) of text, HTML, attachments, etc.
"""
import os
import base64
from pathlib import Path
from email.utils import parsedate_to_datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from config.settings import settings


# What permissions we need. "readonly" = we can read but not send/delete.
# WHY readonly? Principle of least privilege. Don't ask for more than you need.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

TOKEN_PATH = "config/token.json"

def _restore_token_from_env():
    """
    If GMAIL_TOKEN_B64 is set, decode it and write it to config/token.json.
    This lets us ship the token into containerized environments (Railway, Docker)
    without baking it into the image or committing it to git.

    Called at module load. Idempotent — if the token already exists on disk,
    it gets overwritten with the env var version (the env var is source of truth
    in container environments).
    """
    token_b64 = os.environ.get("GMAIL_TOKEN_B64")
    if not token_b64:
        return  # No env var → assume local dev, use existing file if any

    token_bytes = base64.b64decode(token_b64)
    token_path = Path(TOKEN_PATH)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_bytes(token_bytes)


# Call at module import time so the token is ready before anyone calls get_gmail_service
_restore_token_from_env()


def get_gmail_service():
    """
    Authenticate with Gmail and return an API service object.
    
    FLOW (first time):
    1. Open browser → Google login page
    2. User clicks "Allow"
    3. Google gives us a token
    4. We save token to disk
    
    FLOW (subsequent times):
    1. Load saved token
    2. If expired, refresh it automatically
    3. Done — no browser needed
    """
    creds = None
    
    # Try to load existing token
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    
    # If no valid creds, authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Token expired but we have a refresh token → auto-refresh
            print("🔄 Refreshing expired token...")
            creds.refresh(Request())
        else:
            # First time → full OAuth flow (opens browser)
            print("🔐 Opening browser for Gmail authentication...")
            flow = InstalledAppFlow.from_client_secrets_file(
                settings.GMAIL_CREDENTIALS_PATH, SCOPES
            )
            creds = flow.run_local_server(port=0)
        
        # Save for next time
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())
        print("✅ Token saved!")
    
    return build("gmail", "v1", credentials=creds)


def fetch_unread_emails(max_results: int = 10) -> list[dict]:
    """
    Fetch the last N unread emails.
    
    Returns a list of dicts with: subject, sender, date, snippet, body_preview
    """
    service = get_gmail_service()
    
    # Search for unread emails in inbox
    # WHY labelIds AND q? Belt and suspenders.
    # labelIds filters server-side (fast). q adds the search query.
    results = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        q="is:unread",
        maxResults=max_results,
    ).execute()
    
    messages = results.get("messages", [])
    
    if not messages:
        print("📭 No unread emails!")
        return []
    
    emails = []
    
    for msg_ref in messages:
        # msg_ref only has the ID. We need to fetch the full message.
        # WHY format=full? Because "minimal" doesn't include headers/body.
        msg = service.users().messages().get(
            userId="me",
            id=msg_ref["id"],
            format="full",
        ).execute()
        
        # Parse headers
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        
        # Extract body
        body = extract_body(msg["payload"])
        
        email_data = {
            "subject": headers.get("Subject", "(no subject)"),
            "sender": headers.get("From", "(unknown)"),
            "date": headers.get("Date", ""),
            "snippet": msg.get("snippet", ""),  # Google's auto-summary
            "body_preview": body[:500] if body else "(no body)",
        }
        
        emails.append(email_data)
    
    return emails


def extract_body(payload: dict) -> str:
    """
    Extract readable text from email payload.
    
    WHY is this complicated? Because emails use MIME format:
    
    Simple email:
        payload.body.data = "Hello!"
    
    Complex email:
        payload.parts[0] = text/plain → "Hello!"
        payload.parts[1] = text/html  → "<p>Hello!</p>"
        payload.parts[2] = image/png  → attachment
    
    We prefer text/plain. If not available, we take what we can get.
    """
    # Case 1: Simple email with direct body
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    
    # Case 2: Multipart email — dig through parts
    parts = payload.get("parts", [])
    
    for part in parts:
        mime_type = part.get("mimeType", "")
        
        # Prefer plain text
        if mime_type == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
    
    # Fallback: try HTML
    for part in parts:
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
    
    # Nested multipart (e.g., multipart/alternative inside multipart/mixed)
    for part in parts:
        if "parts" in part:
            result = extract_body(part)  # Recurse!
            if result:
                return result
    
    return ""


# ---------- Main ----------

if __name__ == "__main__":
    print("📧 Fetching last 10 unread emails...\n")
    
    emails = fetch_unread_emails(10)
    
    for i, email in enumerate(emails, 1):
        print(f"{'='*60}")
        print(f"📨 Email {i}")
        print(f"   From:    {email['sender']}")
        print(f"   Subject: {email['subject']}")
        print(f"   Date:    {email['date']}")
        print(f"   Preview: {email['snippet'][:100]}...")
        print()
    
    print(f"\n✅ Found {len(emails)} unread emails.")
