"""Gmail API client for creating drafts (never sending). Uses a standard OAuth "installed
app" refresh token — GitHub Actions can't use the interactive Gmail MCP connector, so this
pipeline authenticates directly against the Gmail API instead.

Credentials come from GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN, minted
once via scripts/get_gmail_refresh_token.py (see README.md).
"""

from __future__ import annotations

import base64
import os
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

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


def create_draft(
    to_email: str,
    subject: str,
    body: str,
    attachment_paths: list[Path] | None = None,
    from_email: str | None = None,
) -> str:
    """Creates a Gmail draft (status: draft, never sent). Returns the draft id.

    attachment_paths is optional — a missing or unreadable file is skipped rather than
    failing the whole draft, so one bad path can't block outreach for a lead.

    from_email must be a verified "send as" alias on this Gmail account (Settings ->
    Accounts -> Send mail as) — confirmed working for hm@relianmfg.com. An unverified
    address here would silently fall back to the account's primary address.
    """
    service = build("gmail", "v1", credentials=_credentials())

    message = MIMEMultipart()
    message["to"] = to_email
    if from_email:
        message["from"] = from_email
    message["subject"] = subject
    message.attach(MIMEText(body))

    for path in attachment_paths or []:
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        part = MIMEImage(data)  # all catalogue attachments are jpg product photos
        part.add_header("Content-Disposition", "attachment", filename=Path(path).name)
        message.attach(part)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    draft = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw}})
        .execute()
    )
    return draft["id"]
