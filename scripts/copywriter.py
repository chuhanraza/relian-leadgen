"""Stage 3: Copywriter.

Only runs on leads with a real found email (status='researched', contact_email set,
research_notes set). Drafts a short, specific pitch referencing the actual research_notes
detail. Technical claims are pulled ONLY from config/real_specs_<vertical>.json, and ONLY
if verified_by_hamad is true — otherwise the draft uses a generic capability statement and
research_notes gets "AWAITING SPEC VERIFICATION" appended, so it's visible in review.

Also picks 1-2 relevant product photos from config/catalogue_<vertical>.json for the
Sender to attach — the model can't see the images, so each one was tagged with a short
text description once, up front, by actually looking at it.

Usage:
  python scripts/copywriter.py moto_apparel
  python scripts/copywriter.py combat_sports
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from common.db import get_client
from common.groq_client import generate
from common.parsing import extract_json

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
- Naturally mention that a couple of product photos are attached for reference — one short
  clause, not a separate paragraph.
- End with a light, low-pressure call to action (e.g. offer a sample or a short call).
- Do NOT add any sign-off or signature line — that gets appended separately.
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
- Naturally mention that a couple of product photos are attached for reference — one short
  clause, not a separate paragraph.
- End with a light, low-pressure call to action (e.g. offer a sample or a short call).
- Do NOT add any sign-off or signature line — that gets appended separately.
- Output ONLY the email body text (no subject line, no preamble, no markdown).
"""

IMAGE_SELECT_PROMPT = """A lead named "{brand_name}" is getting a manufacturing pitch email.
What we know about them: "{research_notes}"

Here is our available product photo library for this vertical (filename: description):
{catalogue_list}

Pick the 1-2 images most relevant to this specific lead (e.g. if they mention gloves, pick
glove photos; if unclear, pick a representative mix). Reply with ONLY a fenced ```json code
block: a JSON array of 1-2 filename strings taken exactly from the list above. No other text.
"""

SIGNATURE = "\n\nBest regards,\nHamad, Relian MFG"

SUBJECT_TEMPLATES = {
    "moto_apparel": "Manufacturing partner for {brand_name}?",
    "combat_sports": "Manufacturing partner for {brand_name} gear?",
}


def load_specs(vertical: str) -> dict:
    path = CONFIG_DIR / f"real_specs_{vertical}.json"
    return json.loads(path.read_text())


def load_catalogue(vertical: str) -> list[dict]:
    path = CONFIG_DIR / f"catalogue_{vertical}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def select_images(vertical: str, brand_name: str, research_notes: str, catalogue: list[dict]) -> list[str]:
    if not catalogue:
        return []
    catalogue_list = "\n".join(f"- {c['filename']}: {c['description']}" for c in catalogue)
    prompt = IMAGE_SELECT_PROMPT.format(
        brand_name=brand_name, research_notes=research_notes, catalogue_list=catalogue_list
    )
    valid_filenames = {c["filename"] for c in catalogue}
    try:
        raw = generate(prompt, max_tokens=256)
        picked = extract_json(raw)
    except Exception as exc:  # noqa: BLE001 — bad image pick shouldn't block the draft
        print(f"[copywriter] image selection failed for {brand_name!r}: {exc}")
        return []
    return [f for f in picked if f in valid_filenames][:2]


def run(vertical: str) -> None:
    db = get_client()
    specs = load_specs(vertical)
    verified = specs.get("verified_by_hamad", False)
    catalogue = load_catalogue(vertical)

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

        body = generate(prompt).strip() + SIGNATURE
        subject = SUBJECT_TEMPLATES[vertical].format(brand_name=lead["brand_name"])
        images = select_images(vertical, lead["brand_name"], lead["research_notes"], catalogue)

        db.table("outreach_leads").update(
            {
                "draft_subject": subject,
                "draft_body": body,
                "catalogue_images": images,
                "research_notes": lead["research_notes"] + notes_suffix,
                "status": "drafted",
                "drafted_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", lead["id"]).execute()

        print(f"[copywriter] drafted {lead['domain']} images={images}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("moto_apparel", "combat_sports"):
        sys.exit("Usage: python scripts/copywriter.py <moto_apparel|combat_sports>")
    run(sys.argv[1])
