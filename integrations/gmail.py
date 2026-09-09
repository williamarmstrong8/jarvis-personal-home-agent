"""
J.A.R.V.I.S. — Gmail Integration
"""

import base64
import os
from email.mime.text import MIMEText

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from core.paths import CREDENTIALS

load_dotenv()

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"

# Full scopes: send/compose + read
SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

CREDENTIALS_PATH = os.environ.get(
    "GMAIL_CREDENTIALS_PATH", str(CREDENTIALS / "gmail_credentials.json")
)
TOKEN_PATH       = str(CREDENTIALS / "gmail_token.json")

_service = None


def _get_service():
    global _service
    if _service is not None:
        return _service

    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"\n{RED}Gmail credentials not found at '{CREDENTIALS_PATH}'.{RESET}"
                )
            flow  = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())

    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service


def _build_message(to: str, subject: str, body: str) -> dict:
    msg = MIMEText(body, "plain")
    msg["to"]      = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}


def draft_email(to: str, subject: str, body: str) -> str:
    try:
        svc  = _get_service()
        msg  = _build_message(to, subject, body)
        svc.users().drafts().create(userId="me", body={"message": msg}).execute()
        print(f"{GREEN}[GMAIL] Draft created for {to}{RESET}", flush=True)
        return f"Draft created for {to}, sir."
    except Exception as exc:
        print(f"{RED}[GMAIL] draft error: {exc}{RESET}", flush=True)
        return f"Could not create draft, sir: {exc}"


def send_email(to: str, subject: str, body: str) -> str:
    try:
        svc = _get_service()
        msg = _build_message(to, subject, body)
        svc.users().messages().send(userId="me", body=msg).execute()
        print(f"{GREEN}[GMAIL] Email sent to {to}{RESET}", flush=True)
        return f"Email sent to {to}, sir."
    except Exception as exc:
        print(f"{RED}[GMAIL] send error: {exc}{RESET}", flush=True)
        return f"Could not send email, sir: {exc}"


def _header(headers: list, name: str) -> str:
    """Extract a header value by name from a Gmail message headers list."""
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def search_emails(query: str, max_results: int = 5) -> str:
    """
    Search Gmail with any query string (supports Gmail search operators).
    Returns a formatted summary of matching messages.
    """
    try:
        svc     = _get_service()
        results = svc.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            return f"No emails found for query: '{query}', sir."

        summaries = []
        for m in messages:
            msg     = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]
            ).execute()
            headers = msg.get("payload", {}).get("headers", [])
            sender  = _header(headers, "From")
            subject = _header(headers, "Subject")
            date    = _header(headers, "Date")
            snippet = msg.get("snippet", "")[:150]
            summaries.append(
                f"From: {sender}\nSubject: {subject}\nDate: {date}\nSnippet: {snippet}"
            )

        print(f"{GREEN}[GMAIL] Found {len(summaries)} emails for '{query}'{RESET}", flush=True)
        return "\n\n---\n\n".join(summaries)

    except Exception as exc:
        print(f"{RED}[GMAIL] search error: {exc}{RESET}", flush=True)
        return f"Could not search emails, sir: {exc}"


def read_email(message_id: str) -> str:
    """
    Fetch the full body of a specific Gmail message by ID.
    Returns plain-text content (truncated to 2000 chars).
    """
    try:
        svc = _get_service()
        msg = svc.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()

        headers = msg.get("payload", {}).get("headers", [])
        sender  = _header(headers, "From")
        subject = _header(headers, "Subject")
        date    = _header(headers, "Date")

        # Walk parts to find plain-text body
        def _extract_body(payload: dict) -> str:
            mime = payload.get("mimeType", "")
            if mime == "text/plain":
                data = payload.get("body", {}).get("data", "")
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            for part in payload.get("parts", []):
                result = _extract_body(part)
                if result:
                    return result
            return ""

        body = _extract_body(msg.get("payload", {}))
        body = body.strip()[:2000]

        print(f"{GREEN}[GMAIL] Read message {message_id}{RESET}", flush=True)
        return f"From: {sender}\nSubject: {subject}\nDate: {date}\n\n{body}"

    except Exception as exc:
        print(f"{RED}[GMAIL] read error: {exc}{RESET}", flush=True)
        return f"Could not read email, sir: {exc}"
