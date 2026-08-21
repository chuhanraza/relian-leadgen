"""EICMA invitation campaign. Separate from the moto_apparel/combat_sports cold-outreach
pipeline — reads/writes leadgen.eicma_invitations + leadgen.eicma_campaign_state only.

Sends (drafts) up to DAILY_BATCH_SIZE invitations per run, always excluding
suppressed=true contacts. Never contacted contacts go first, then oldest last_sent_at,
so the list gets even coverage. When every non-suppressed contact has been drafted at
least once in the current pass, advances current_wave and resets last_sent_at so the
next run starts a fresh pass — under a new wave design if one exists on disk.

Same hard rule as the rest of this repo: only ever calls gmail drafts().create, never
messages().send. Hamad reviews and bulk-sends drafts manually.

Usage:
  python scripts/eicma_campaign.py
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from common.db import get_client
from common.gmail_client import create_draft
from common.groq_client import MODEL_FAST, generate

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
WAVES_DIR = BASE_DIR / "config" / "eicma_waves"

DAILY_BATCH_SIZE = 20
FROM_EMAIL = "hm@relianmfg.com"

GREETING_PROMPT = """This is a messy CRM contact name field: "{contact_name}"
It may contain a person's name, a company name, both, or neither cleanly.
Extract the single best word or short phrase to greet them with in a
business email opener — a first name if one is clearly present, otherwise
a short/clean version of the company name (max 3 words), otherwise the
word "there" if nothing usable exists. Reply with ONLY that word/phrase,
nothing else."""


def extract_greeting(contact_name: str) -> str:
    prompt = GREETING_PROMPT.format(contact_name=contact_name)
    try:
        text = generate(prompt, max_tokens=150, model=MODEL_FAST).strip().strip('"').rstrip(".")
    except Exception as exc:  # noqa: BLE001 — a bad greeting extraction shouldn't block a draft
        print(f"[eicma_campaign] greeting extraction failed for {contact_name!r}: {exc}")
        return "there"
    # Small models don't always match the fallback word's exact casing/punctuation
    # ("There.", "THERE") — normalize so it never reads as a formatting glitch in the draft.
    if text.lower() == "there":
        return "there"
    return text or "there"


def available_waves() -> list[int]:
    numbers = []
    for path in WAVES_DIR.glob("wave_*.html"):
        try:
            n = int(path.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        if (WAVES_DIR / f"wave_{n}.json").exists():
            numbers.append(n)
    return sorted(numbers)


def load_wave(wave_number: int) -> tuple[int, str, str]:
    """Returns (actual_wave_used, html, subject). Falls back to the highest wave on disk
    and prints a NEEDS NEW WAVE DESIGN notice if wave_number isn't available yet."""
    waves = available_waves()
    if not waves:
        raise SystemExit(f"[eicma_campaign] no wave templates found in {WAVES_DIR}")

    actual = wave_number if wave_number in waves else max(waves)
    if actual != wave_number:
        print(
            f"[eicma_campaign] NEEDS NEW WAVE DESIGN — wave_{wave_number} not found, "
            f"falling back to wave_{actual} (highest available)."
        )

    html = (WAVES_DIR / f"wave_{actual}.html").read_text(encoding="utf-8")
    subject = json.loads((WAVES_DIR / f"wave_{actual}.json").read_text(encoding="utf-8"))["subject"]
    return actual, html, subject


def get_campaign_state(db) -> int:
    row = db.table("eicma_campaign_state").select("current_wave").eq("id", 1).maybe_single().execute().data
    return row["current_wave"] if row else 1


def run() -> None:
    db = get_client()
    current_wave = get_campaign_state(db)
    actual_wave, html_template, subject = load_wave(current_wave)

    contacts = (
        db.table("eicma_invitations")
        .select("*")
        .eq("suppressed", False)
        .order("last_sent_at", desc=False, nullsfirst=True)
        .limit(DAILY_BATCH_SIZE)
        .execute()
        .data
    )
    print(f"[eicma_campaign] wave={actual_wave} batch={len(contacts)}")

    for contact in contacts:
        greeting = extract_greeting(contact["contact_name"] or "")
        rendered_html = html_template.replace("{{GREETING}}", greeting)

        try:
            draft_id = create_draft(
                to_email=contact["email"],
                subject=subject,
                body=rendered_html,
                from_email=FROM_EMAIL,
                is_html=True,
            )
        except Exception as exc:  # noqa: BLE001 — one failed draft shouldn't block the rest
            print(f"[eicma_campaign] failed for {contact['email']}: {exc}")
            continue

        db.table("eicma_invitations").update(
            {
                "last_sent_at": datetime.now(timezone.utc).isoformat(),
                "status": "drafted",
                "last_wave_design": f"wave_{actual_wave}",
                "cycle_number": (contact.get("cycle_number") or 0) + 1,
            }
        ).eq("id", contact["id"]).execute()
        print(f"[eicma_campaign] drafted {draft_id} for {contact['email']} greeting={greeting!r}")

    remaining = (
        db.table("eicma_invitations")
        .select("id", count="exact")
        .eq("suppressed", False)
        .is_("last_sent_at", "null")
        .execute()
        .count
    )
    if remaining == 0:
        next_wave = current_wave + 1
        db.table("eicma_campaign_state").update({"current_wave": next_wave}).eq("id", 1).execute()
        db.table("eicma_invitations").update({"last_sent_at": None}).eq("suppressed", False).execute()
        print(
            f"[eicma_campaign] full pass complete — advancing to wave_{next_wave}, "
            "reset last_sent_at for next pass"
        )


if __name__ == "__main__":
    run()
