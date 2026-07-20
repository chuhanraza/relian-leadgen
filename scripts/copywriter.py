"""Stage 3: Copywriter.

Only runs on leads with a real found email (status='researched', contact_email set,
research_notes set). Drafts a short, specific pitch referencing the actual research_notes
detail. Technical claims are pulled ONLY from config/real_specs_<vertical>.json, and ONLY
if verified_by_hamad is true — otherwise the draft uses a generic capability statement and
research_notes gets "AWAITING SPEC VERIFICATION" appended, so it's visible in review.

Usage:
  python scripts/copywriter.py moto_apparel
  python scripts/copywriter.py combat_sports
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from common.claude_client import generate
from common.db import get_client

load_dotenv()

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

PROMPT_VERIFIED = """Write a short, specific cold outreach email pitching manufacturing
partnership from Relian MFG (Sialkot, Pakistan) to {brand_name}.

Reference this real, specific detail about them — do not write a generic template:
"{research_notes}"

You may reference these VERIFIED manufacturing capabilities where relevant:
{specs_json}

Rules:
- 120-180 words. No bullet lists of every spec — weave in only what's relevant to a
  reasonable outreach pitch a busy person will actually read.
- End with a light, low-pressure call to action (e.g. offer a sample or a short call).
- Sign the email exactly as: "Hamad, Relian MFG"
- Output ONLY the email body text (no subject line, no preamble, no markdown).
"""

PROMPT_UNVERIFIED = """Write a short, specific cold outreach email pitching manufacturing
partnership from Relian MFG (Sialkot, Pakistan) to {brand_name}.

Reference this real, specific detail about them — do not write a generic template:
"{research_notes}"

Do NOT state any specific material, certification, MOQ, or lead-time claim — those are not
yet verified. Use only a generic capability statement (e.g. "we manufacture technical
apparel/gear to spec for brands like yours" — no numbers, no named materials).

Rules:
- 100-150 words. Reference the specific detail above naturally.
- End with a light, low-pressure call to action (e.g. offer a sample or a short call).
- Sign the email exactly as: "Hamad, Relian MFG"
- Output ONLY the email body text (no subject line, no preamble, no markdown).
"""

SUBJECT_TEMPLATES = {
    "moto_apparel": "Manufacturing partner for {brand_name}?",
    "combat_sports": "Manufacturing partner for {brand_name} gear?",
}


def load_specs(vertical: str) -> dict:
    path = CONFIG_DIR / f"real_specs_{vertical}.json"
    return json.loads(path.read_text())


def run(vertical: str) -> None:
    db = get_client()
    specs = load_specs(vertical)
    verified = specs.get("verified_by_hamad", False)

    leads = (
        db.table("outreach_leads")
        .select("*")
        .eq("vertical", vertical)
        .eq("status", "researched")
        .eq("contact_method", "email")
        .not_.is_("contact_email", "null")
        .not_.is_("research_notes", "null")
        .execute()
        .data
    )
    print(f"[copywriter] vertical={vertical} pending={len(leads)} verified_by_hamad={verified}")

    for lead in leads:
        if verified:
            prompt = PROMPT_VERIFIED.format(
                brand_name=lead["brand_name"],
                research_notes=lead["research_notes"],
                specs_json=json.dumps(specs, indent=2),
            )
            notes_suffix = ""
        else:
            prompt = PROMPT_UNVERIFIED.format(
                brand_name=lead["brand_name"], research_notes=lead["research_notes"]
            )
            notes_suffix = " AWAITING SPEC VERIFICATION."

        body = generate(prompt).strip()
        subject = SUBJECT_TEMPLATES[vertical].format(brand_name=lead["brand_name"])

        db.table("outreach_leads").update(
            {
                "draft_subject": subject,
                "draft_body": body,
                "research_notes": lead["research_notes"] + notes_suffix,
                "status": "drafted",
                "drafted_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", lead["id"]).execute()

        print(f"[copywriter] drafted {lead['domain']}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("moto_apparel", "combat_sports"):
        sys.exit("Usage: python scripts/copywriter.py <moto_apparel|combat_sports>")
    run(sys.argv[1])
