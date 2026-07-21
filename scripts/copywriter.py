"""Stage 3: Copywriter.

Only runs on leads with a real found email. combat_sports uses a deterministic
template built from config/real_specs_combat_sports.json (Hamad's real example
structure) with the LLM scoped to ONLY the opening personalization line, so the
three named models, subject, and CTA never drift from what's verified. moto_apparel
still uses the older open-ended prompt pending its own spec verification.

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

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
ASSETS_DIR = BASE_DIR / "assets" / "catalogue"

SIGNATURE = "\n\nHamad, Relian MFG"

# ---------- moto_apparel: unchanged, still open-ended pending spec verification ----------

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

SUBJECT_TEMPLATES = {
    "moto_apparel": "Manufacturing partner for {brand_name}?",
}

# ---------- combat_sports: deterministic template, LLM only writes the opening line ----------

COMBAT_SUBJECT = "Question regarding hand wraps stock"

COMBAT_OPENING_LINE_PROMPT = """A lead named "{brand_name}" is getting a cold outreach
email about private-label hand wraps. Here's what we actually found out about them:
"{research_notes}"

Write ONE short, plain sentence (max 25 words) to open the email with, in the voice of
someone who's looked at their business specifically, not a mass mailer. Ground it in the
real detail above if it's genuinely usable. If the detail isn't specific/useful enough to
build a real sentence from, write a soft, generic-but-honest opener instead — e.g.
"I follow your brand and wanted to check whether your current hand wrap lineup has room
for something new." NEVER assert that their current product or supplier is subpar, low
quality, or outdated — we have no evidence of that and it reads as a lie if untrue.

Output ONLY that one sentence. No quotes, no preamble, no signature.
"""


def build_combat_sports_email(brand_name: str, research_notes: str, specs: dict) -> tuple[str, str]:
    opening = generate(
        COMBAT_OPENING_LINE_PROMPT.format(brand_name=brand_name, research_notes=research_notes),
        max_tokens=100,
    ).strip()

    model_lines = []
    for i, m in enumerate(specs.get("materials", []), start=1):
        model_lines.append(f'{i}. **{m["name"]}:** {m["notes"]}')
    models_block = "\n".join(model_lines)

    lookbook_path = ASSETS_DIR / "lookbook_hand_wraps.pdf"
    next_step_line = (
        "\n\n**Next Step:** I have attached our Lookbook for hand wraps."
        if lookbook_path.exists()
        else ""
    )

    body = (
        f"Hi Sir / Madam,\n\n"
        f"{opening}\n\n"
        f'We manufacture private label "Boutique-Grade" wraps for brands that want to '
        f"dominate the market. We have multiple specialized models ready for your 2026 "
        f"collection:\n\n{models_block}"
        f"{next_step_line}\n\n"
        f"Do you have 5 minutes this week to discuss which model fits your brand? "
        f"I would share my full catalogue once you request."
        f"{SIGNATURE}"
    )
    return COMBAT_SUBJECT, body


# ---------- shared: image selection ----------

IMAGE_SELECT_PROMPT = """A lead named "{brand_name}" is getting a manufacturing pitch email.
What we know about them: "{research_notes}"

Here is our available product photo library for this vertical (filename: description):
{catalogue_list}

Pick the 1-2 images most relevant to this specific lead (e.g. if they mention gloves, pick
glove photos; if unclear, pick a representative mix). Reply with ONLY a fenced ```json code
block: a JSON array of 1-2 filename strings taken exactly from the list above. No other text.
"""


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
        try:
            notes_suffix = ""

            if vertical == "combat_sports" and verified:
                subject, body = build_combat_sports_email(
                    lead["brand_name"], lead["research_notes"], specs
                )
            elif verified:
                prompt = PROMPT_VERIFIED.format(
                    brand_name=lead["brand_name"],
                    research_notes=lead["research_notes"],
                    specs_json=json.dumps(specs, indent=2),
                )
                body = generate(prompt).strip() + SIGNATURE
                subject = SUBJECT_TEMPLATES[vertical].format(brand_name=lead["brand_name"])
            else:
                prompt = PROMPT_UNVERIFIED.format(
                    brand_name=lead["brand_name"], research_notes=lead["research_notes"]
                )
                body = generate(prompt).strip() + SIGNATURE
                subject = SUBJECT_TEMPLATES.get(
                    vertical, "Manufacturing partner for {brand_name}?"
                ).format(brand_name=lead["brand_name"])
                notes_suffix = " AWAITING SPEC VERIFICATION."
        except Exception as exc:  # noqa: BLE001 — e.g. Groq rate limit; retry next run, don't
            # crash the whole job and skip every remaining lead plus the Sender/Summary steps.
            print(f"[copywriter] failed for {lead['domain']}: {exc}")
            continue

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
