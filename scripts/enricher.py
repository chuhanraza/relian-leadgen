"""Stage 2: Enricher.

For every status='researched' lead that hasn't been enriched yet (research_notes IS NULL),
fetches the brand's site + one social profile via web search, pulls 1-2 genuine specific
details, and attempts to find a real published contact email. Never guesses an email
pattern — no email found means status='skipped_no_email' and the lead does not proceed to
the Copywriter.

Usage:
  python scripts/enricher.py moto_apparel
  python scripts/enricher.py combat_sports
"""

import sys

from dotenv import load_dotenv

from common.gemini_client import research
from common.db import get_client
from common.parsing import extract_json

load_dotenv()

PROMPT = """Research this company for a B2B outreach pitch: {brand_name} ({website_url}).

1. Find 1-2 GENUINE, specific details from their own website or a social profile —
   positioning, materials they currently use, or any stated values like "made
   domestically"/"made in USA"/"handmade locally". These must be real, verifiable
   specifics, not generic guesses. If the brand markets itself as domestically-made or
   in-house manufactured, note that explicitly — it makes them a weaker outsourcing
   prospect, so flag it rather than omitting it.

2. Find a REAL, PUBLISHED contact email from their site (contact page, about page,
   footer) or a linked LinkedIn/social profile. Do NOT guess or construct a pattern email
   (e.g. do not invent info@domain.com unless you actually saw it published). If you
   cannot find one, say so — do not fabricate.

Reply with ONLY a fenced ```json code block, a single object with exactly these keys:
- research_notes: string, 1-3 sentences with the specific detail(s) found
- domestically_made: boolean, true if they market themselves as domestic/in-house made
- contact_email: string with the real published email, or null if none was found
No other text.
"""


def run(vertical: str) -> None:
    db = get_client()
    leads = (
        db.table("outreach_leads")
        .select("*")
        .eq("vertical", vertical)
        .eq("status", "researched")
        .is_("research_notes", "null")
        .execute()
        .data
    )
    print(f"[enricher] vertical={vertical} pending={len(leads)}")

    for lead in leads:
        prompt = PROMPT.format(
            brand_name=lead["brand_name"], website_url=lead.get("website_url") or lead["domain"]
        )
        try:
            raw = research(prompt, max_uses=5)
            data = extract_json(raw)
        except Exception as exc:  # noqa: BLE001 — a single lead's research failure shouldn't kill the run
            print(f"[enricher] failed for {lead['domain']}: {exc}")
            continue

        notes = data.get("research_notes", "").strip()
        if data.get("domestically_made"):
            notes += " (Flag: brand markets itself as domestically/in-house made — weaker outsourcing prospect.)"

        email = data.get("contact_email")
        if email and "@" in email:
            db.table("outreach_leads").update(
                {
                    "research_notes": notes,
                    "contact_email": email,
                    "contact_method": "email",
                }
            ).eq("id", lead["id"]).execute()
        else:
            db.table("outreach_leads").update(
                {
                    "research_notes": notes,
                    "contact_method": "contact_form_manual_needed",
                    "status": "skipped_no_email",
                }
            ).eq("id", lead["id"]).execute()

        print(f"[enricher] {lead['domain']}: email_found={bool(email)}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("moto_apparel", "combat_sports"):
        sys.exit("Usage: python scripts/enricher.py <moto_apparel|combat_sports>")
    run(sys.argv[1])
