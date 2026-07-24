"""Gmail API client for creating drafts (never sending). Uses a standard OAuth "installed
app" refresh token — GitHub Actions can't use the interactive Gmail MCP connector, so this
pipeline authenticates directly against the Gmail API instead.

Credentials come from GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN, minted
once via scripts/get_gmail_refresh_token.py (see README.md).
"""

from __future__ import annotations

import base64
import html
import os
import re
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def _plain_text_to_html(text: str) -> str:
    """Minimal, safe plain-text -> HTML conversion: escapes special characters and turns
    blank-line-separated paragraphs into <p> blocks (single linebreaks within a paragraph
    become <br>), so a plain cold-outreach body reads the same once it has to become HTML
    to carry an inline image.
    """
    escaped = html.escape(text)
    paragraphs = re.split(r"\n\s*\n", escaped.strip())
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)


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
    is_html: bool = False,
    inline_image_path: Path | None = None,
    inline_image_cid: str = "banner-image",
) -> str:
    """Creates a Gmail draft (status: draft, never sent). Returns the draft id.

    attachment_paths is optional — a missing or unreadable file is skipped rather than
    failing the whole draft, so one bad path can't block outreach for a lead. These are
    real downloadable attachments, distinct from inline_image_path below.

    from_email must be a verified "send as" alias on this Gmail account (Settings ->
    Accounts -> Send mail as) — confirmed working for hm@relianmfg.com. An unverified
    address here would silently fall back to the account's primary address.

    is_html=True sends body as text/html (e.g. a full campaign template) instead of the
    default text/plain used by the cold-outreach pipeline's short drafted pitches.

    inline_image_path, if given, embeds that image IN the email body (visible inline,
    not a separate attachment) via a cid: reference — Gmail's standard
    multipart/related-inside-multipart/mixed structure. This forces the body to HTML
    (converting a plain-text body automatically via _plain_text_to_html if is_html is
    False) since a plain-text body can't carry an inline image. A missing/unreadable
    inline_image_path is skipped the same as any attachment_paths entry, falling back to
    a plain body with no image rather than failing the whole draft.
    """
    service = build("gmail", "v1", credentials=_credentials())

    message = MIMEMultipart("mixed")
    message["to"] = to_email
    if from_email:
        message["from"] = from_email
    message["subject"] = subject

    inline_data = None
    if inline_image_path is not None:
        try:
            inline_data = Path(inline_image_path).read_bytes()
        except OSError:
            inline_data = None

    if inline_data is not None:
        html_body = body if is_html else _plain_text_to_html(body)
        html_body += (
            f'<br><img src="cid:{inline_image_cid}" alt="" '
            'style="max-width:600px;width:100%;height:auto;margin-top:16px;">'
        )
        related = MIMEMultipart("related")
        related.attach(MIMEText(html_body, "html"))
        image_part = MIMEImage(inline_data)
        image_part.add_header("Content-ID", f"<{inline_image_cid}>")
        image_part.add_header(
            "Content-Disposition", "inline", filename=Path(inline_image_path).name
        )
        related.attach(image_part)
        message.attach(related)
    else:
        message.attach(MIMEText(body, "html" if is_html else "plain"))

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
