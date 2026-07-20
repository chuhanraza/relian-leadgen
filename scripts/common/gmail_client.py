"""Gmail API client for creating drafts (never sending). Uses a standard OAuth "installed
app" refresh token — GitHub Actions can't use the interactive Gmail MCP connector, so this
pipeline authenticates directly against the Gmail API instead.

Credentials come from GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN, minted
once via scripts/get_gmail_refresh_token.py (see README.md).
"""

import base64
import os
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def _credentials() -> Credentials:
    return Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


def create_draft(to_email: str, subject: str, body: str) -> str:
    """Creates a Gmail draft (status: draft, never sent). Returns the draft id."""
    service = build("gmail", "v1", credentials=_credentials())

    message = MIMEText(body)
    message["to"] = to_email
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    draft = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw}})
        .execute()
    )
    return draft["id"]
