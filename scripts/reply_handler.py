"""Stage 6 (standalone, not part of the daily pipeline): Reply Handler.

Finds customer replies to threads we started (moto_apparel, combat_sports, or EICMA
outreach), classifies each reply, and drafts a response — as a Gmail DRAFT, never sent,
same hard rule as every other script in this pipeline. A human (Hamad) reviews and sends
every draft manually.

Runs against the single relianmfg@gmail.com mailbox — hm@relianmfg.com and
hm@reliansports.com are both verified "send as" aliases on that one account, so one
OAuth credential covers searching/reading/drafting across both.

Dedup: leadgen.handled_replies.message_id (Gmail's internal message id, primary key) —
a reply already recorded there is never redrafted, even across repeated runs.

Usage:
  python scripts/reply_handler.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from dotenv import load_dotenv

from common.db import get_client
from common.gmail_client import (
    create_reply_draft,
    find_unreplied_customer_threads,
    get_header,
    get_message_text,
)
from common.groq_client import MODEL_FAST, generate

load_dotenv()

OUR_ADDRESSES = ["hm@relianmfg.com", "hm@reliansports.com"]

CAMPAIGN_COMPANY = {
    "moto_apparel": "Relian MFG",
    "eicma": "Relian MFG",
    "combat_sports": "Relian Sports",
    "unknown": "Relian MFG",
}

VALID_CLASSIFICATIONS = {"confirmed_interested", "declined", "asking_question", "other"}

CLASSIFY_PROMPT = """Classify a customer's reply to a cold outreach email into EXACTLY
ONE of these categories:
- confirmed_interested (they are agreeing to meet/visit/sample, or otherwise saying yes)
- declined (a polite no / not interested)
- asking_question (they want pricing, MOQ, catalogue, samples, or other specifics)
- other (anything that doesn't clearly fit the above)

Original email we sent:
{original_email}

Their reply:
{their_reply}

Reply with ONLY the category name, exactly as spelled above. No other text.
"""

REPLY_DRAFT_PROMPT = """You are an excellent, warm, professional sales correspondent for
{company_name}, a garment/glove manufacturer. A prospect replied to our outreach. Write a
genuinely good reply — the kind an excellent, attentive salesperson would write, not a
generic template.

Original email we sent: {original_email}
Their reply: {their_reply}
Classification: {classification}

Rules:
- If confirmed_interested: warmly confirm specifics (dates/location/booth if relevant),
  reference anything specific they mentioned (their company name, products, etc.), no
  re-pitching since they already said yes.
- If declined: gracious, brief, leave the door open, never pushy, never re-pitch.
- If asking_question: answer directly and helpfully using only real information from the
  original email context — never invent pricing, MOQ numbers, or facts not already
  established. If the real answer isn't in context, say you'll follow up with specifics
  rather than guessing.
- Match the register/formality of their reply.
- Sign off as exactly "Hamad, {company_name}" — no other closing line.
- Output ONLY the email body text, no subject line.
"""


def classify_campaign(our_message: dict) -> str:
    subject = get_header(our_message, "Subject")
    from_header = get_header(our_message, "From").lower()
    if "eicma" in subject.lower():
        return "eicma"
    if "hm@relianmfg.com" in from_header:
        return "moto_apparel"
    if "hm@reliansports.com" in from_header:
        return "combat_sports"
    return "unknown"


def classify_reply(original_email: str, their_reply: str) -> str:
    prompt = CLASSIFY_PROMPT.format(original_email=original_email, their_reply=their_reply)
    raw = generate(prompt, max_tokens=150, model=MODEL_FAST).strip().lower()
    for candidate in VALID_CLASSIFICATIONS:
        if candidate in raw:
            return candidate
    print(f"[reply_handler] unrecognized classification {raw!r} — defaulting to 'other'")
    return "other"


def draft_reply_body(
    original_email: str, their_reply: str, classification: str, company_name: str
) -> str:
    prompt = REPLY_DRAFT_PROMPT.format(
        company_name=company_name,
        original_email=original_email,
        their_reply=their_reply,
        classification=classification,
    )
    return generate(prompt, max_tokens=500).strip()


def run() -> None:
    db = get_client()
    already_handled = {
        row["message_id"] for row in db.table("handled_replies").select("message_id").execute().data
    }

    threads = find_unreplied_customer_threads(OUR_ADDRESSES)
    print(f"[reply_handler] candidate threads with an unreplied customer message: {len(threads)}")

    for candidate in threads:
        our_message = candidate["our_message"]
        reply_message = candidate["reply_message"]
        reply_id = reply_message["id"]

        if reply_id in already_handled:
            continue

        campaign = classify_campaign(our_message)
        company_name = CAMPAIGN_COMPANY[campaign]
        from_address = get_header(reply_message, "From")
        original_email = get_message_text(our_message)
        their_reply = get_message_text(reply_message)

        try:
            classification = classify_reply(original_email, their_reply)
            draft_body = draft_reply_body(original_email, their_reply, classification, company_name)

            reply_to_email = get_header(reply_message, "Reply-To") or from_address
            subject = get_header(reply_message, "Subject")
            in_reply_to = get_header(reply_message, "Message-Id") or get_header(
                reply_message, "Message-ID"
            )
            references = get_header(reply_message, "References")
            from_email = "hm@reliansports.com" if campaign == "combat_sports" else "hm@relianmfg.com"

            draft_id = create_reply_draft(
                thread_id=candidate["thread_id"],
                to_email=reply_to_email,
                subject=subject,
                body=draft_body,
                in_reply_to_message_id=in_reply_to,
                references=references,
                from_email=from_email,
            )
        except Exception as exc:  # noqa: BLE001 — one bad thread shouldn't block the rest
            print(f"[reply_handler] failed for thread {candidate['thread_id']}: {exc}")
            continue

        db.table("handled_replies").insert(
            {
                "message_id": reply_id,
                "thread_id": candidate["thread_id"],
                "from_address": from_address,
                "original_campaign": campaign,
                "reply_classification": classification,
                "handled_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()

        print(
            f"[reply_handler] drafted reply {draft_id} for thread {candidate['thread_id']} "
            f"campaign={campaign} classification={classification}"
        )


if __name__ == "__main__":
    run()
