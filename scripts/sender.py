"""Stage 4: Sender. Creates a Gmail DRAFT (never sends) for every drafted lead that
doesn't already have one. Auto-send is explicitly out of scope — see README.md.

gmail_draft_id is the idempotency marker: status stays 'drafted' permanently (the
simplest reliable option — see README's "Sender reconciliation tradeoff" section), so
without this marker a daily re-run would recreate the same draft every time.

Attaches the product photos the Copywriter picked (lead.catalogue_images) from
assets/catalogue/<vertical>/ — a missing/renamed file is skipped, not a hard failure.
combat_sports also gets the standing hand-wraps banner embedded INLINE in the email
body (INLINE_BANNERS, via gmail_client's cid: support) rather than as another attachment.

Drafts send from a per-vertical alias (both confirmed as verified "send as" addresses on
relianmfg@gmail.com) rather than the raw Gmail address.

Usage:
  python scripts/sender.py moto_apparel
  python scripts/sender.py combat_sports
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

from common.db import get_client
from common.gmail_client import create_draft

load_dotenv()

CATALOGUE_DIR = Path(__file__).resolve().parent.parent / "assets" / "catalogue"

FROM_EMAILS = {
    "moto_apparel": "hm@relianmfg.com",
    "combat_sports": "hm@reliansports.com",
}

# combat_sports only: embedded inline (visible in the email body itself, via cid:) on
# every outgoing draft, on top of the 1-2 individually selected product photos below
# (those stay real, separate attachments).
INLINE_BANNERS = {
    "combat_sports": CATALOGUE_DIR / "combat_sports" / "hand_wraps_banner.jpg",
}


def run(vertical: str) -> None:
    db = get_client()
    leads = (
        db.table("outreach_leads")
        .select("*")
        .eq("vertical", vertical)
        .eq("status", "drafted")
        .is_("gmail_draft_id", "null")
        .execute()
        .data
    )
    print(f"[sender] vertical={vertical} pending={len(leads)}")

    for lead in leads:
        attachment_paths = [
            CATALOGUE_DIR / vertical / filename for filename in (lead.get("catalogue_images") or [])
        ]
        inline_banner = INLINE_BANNERS.get(vertical)
        try:
            draft_id = create_draft(
                to_email=lead["contact_email"],
                subject=lead["draft_subject"],
                body=lead["draft_body"],
                attachment_paths=attachment_paths,
                from_email=FROM_EMAILS[vertical],
                inline_image_path=inline_banner,
            )
        except Exception as exc:  # noqa: BLE001 — one failed draft shouldn't block the rest
            print(f"[sender] failed for {lead['domain']}: {exc}")
            continue

        db.table("outreach_leads").update({"gmail_draft_id": draft_id}).eq(
            "id", lead["id"]
        ).execute()
        print(
            f"[sender] created draft {draft_id} for {lead['domain']} "
            f"(attachments={len(attachment_paths)}, inline_banner={'yes' if inline_banner else 'no'})"
        )


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("moto_apparel", "combat_sports"):
        sys.exit("Usage: python scripts/sender.py <moto_apparel|combat_sports>")
    run(sys.argv[1])
