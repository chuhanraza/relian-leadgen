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

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    # Needed by search_reply_threads()/get_thread() below (reply_handler.py) — gmail.compose
    # alone only covers managing drafts, not reading arbitrary INBOX/SENT mail.
    "https://www.googleapis.com/auth/gmail.readonly",
]


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


# ---------- reply handling (scripts/reply_handler.py) — needs gmail.readonly on top of
# gmail.compose to search/read INBOX, unlike everything above this line. ----------


def get_service():
    return build("gmail", "v1", credentials=_credentials())


def get_header(message: dict, name: str) -> str:
    for header in message.get("payload", {}).get("headers", []):
        if header["name"].lower() == name.lower():
            return header["value"]
    return ""


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode() + b"==").decode("utf-8", errors="replace")


def get_message_text(message: dict) -> str:
    """Best-effort plain-text body: prefers text/plain, falls back to a crude tag-strip
    of text/html (replies are read for LLM context, not rendered — a rough strip is fine).
    """
    payload = message.get("payload", {})
    parts = payload.get("parts") or [payload]

    def walk(parts_list: list[dict]) -> tuple[str | None, str | None]:
        plain, html_body = None, None
        for part in parts_list:
            mime = part.get("mimeType", "")
            body_data = part.get("body", {}).get("data")
            if mime == "text/plain" and body_data and plain is None:
                plain = _decode_part(body_data)
            elif mime == "text/html" and body_data and html_body is None:
                html_body = _decode_part(body_data)
            elif part.get("parts"):
                sub_plain, sub_html = walk(part["parts"])
                plain = plain or sub_plain
                html_body = html_body or sub_html
        return plain, html_body

    plain, html_body = walk(parts)
    if plain:
        return plain.strip()
    if html_body:
        return re.sub(r"<[^>]+>", " ", html_body).strip()
    return ""


def find_unreplied_customer_threads(exclude_from_addresses: list[str]) -> list[dict]:
    """Finds Gmail threads where we sent the original message and the customer's reply
    is the newest message in the thread (i.e. we haven't replied yet).

    Returns a list of {"thread_id", "our_message", "reply_message"} dicts — our_message is
    the earliest message in the thread sent from one of exclude_from_addresses (the
    original outbound pitch), reply_message is the newest message overall (the customer's
    unreplied reply). Threads with no outbound message from us, or whose newest message is
    from us, are skipped.
    """
    service = get_service()
    exclude_clause = " ".join(f"-from:{addr}" for addr in exclude_from_addresses)
    query = f"in:inbox {exclude_clause} -in:chats"

    thread_ids: set[str] = set()
    page_token = None
    while True:
        resp = (
            service.users()
            .threads()
            .list(userId="me", q=query, pageToken=page_token, maxResults=100)
            .execute()
        )
        thread_ids.update(t["id"] for t in resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    results = []
    for thread_id in thread_ids:
        thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        messages = sorted(thread.get("messages", []), key=lambda m: int(m["internalDate"]))
        if not messages:
            continue

        newest = messages[-1]
        newest_from = get_header(newest, "From").lower()
        if any(addr.lower() in newest_from for addr in exclude_from_addresses):
            continue  # last word in the thread was already ours

        our_message = next(
            (
                m
                for m in messages
                if any(addr.lower() in get_header(m, "From").lower() for addr in exclude_from_addresses)
            ),
            None,
        )
        if our_message is None:
            continue  # not a thread we started

        results.append({"thread_id": thread_id, "our_message": our_message, "reply_message": newest})

    return results


def create_reply_draft(
    thread_id: str,
    to_email: str,
    subject: str,
    body: str,
    in_reply_to_message_id: str,
    references: str,
    from_email: str,
) -> str:
    """Creates a Gmail draft threaded as a reply to in_reply_to_message_id (the RFC
    Message-ID header value, not the Gmail internal id) within thread_id, so it appears
    inline in the conversation instead of as a new top-level thread.
    """
    service = get_service()

    message = MIMEMultipart("alternative")
    message["to"] = to_email
    message["from"] = from_email
    message["subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to_message_id:
        message["In-Reply-To"] = in_reply_to_message_id
        message["References"] = f"{references} {in_reply_to_message_id}".strip()
    message.attach(MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    draft = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw, "threadId": thread_id}})
        .execute()
    )
    return draft["id"]
