"""EICMA invitation campaign. Separate from the moto_apparel/combat_sports cold-outreach
pipeline — reads/writes leadgen.eicma_invitations only.

Sends (drafts) up to DAILY_BATCH_SIZE invitations per run. Hard limits, enforced in code:
  * suppressed=true contacts are never drafted.
  * a contact is never drafted at cycle_number >= MAX_CYCLES (3 regular waves, then done).
  * a contact is never drafted within MIN_DAYS_BETWEEN (14) days of last_sent_at.
  * the list is never reset or looped — last_sent_at is never cleared and
    eicma_campaign_state.current_wave is no longer advanced (the old pass/reset loop mailed
    contacts up to 11 times).
A contact's wave follows its own cycle: cycle 0 -> wave_1, 1 -> wave_2, 2 -> wave_3.

wave_4 is the single FINAL reminder. It is never used by the regular run; it only runs when
EICMA_FINAL_REMINDER=1 is set, goes to non-suppressed contacts that have not already had
wave_4, still honours the 14-day gap, and is the one deliberate exception to MAX_CYCLES.
Final-mode guards (all in is_eligible/select_batch, shared by the run and --report):
  * suppressed contacts and contacts already sent wave_4 are skipped (also by email address,
    so a duplicate row for the same address can't be mailed twice);
  * helpdesk-style inboxes (support, service, customerservice, help, care, noreply, orders,
    returns) are skipped;
  * 14-day gap since last_sent_at and the 20/day batch cap still apply;
  * no drafts on or after FINAL_CUTOFF (2026-11-02, UTC) in any mode.

Same hard rule as the rest of this repo: only ever calls gmail drafts().create, never
messages().send. Hamad reviews and bulk-sends drafts manually.

Usage:
  python scripts/eicma_campaign.py            # regular run (waves 1-3, capped)
  python scripts/eicma_campaign.py --report   # print eligibility counts, create nothing
  EICMA_FINAL_REMINDER=1 python scripts/eicma_campaign.py   # wave_4 final reminder
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from common.db import get_client
from common.gmail_client import create_draft
from common.groq_client import MODEL_FAST, generate

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
WAVES_DIR = BASE_DIR / "config" / "eicma_waves"
BRANDING_DIR = BASE_DIR / "assets" / "branding"

DAILY_BATCH_SIZE = 20
MAX_CYCLES = 3
MIN_DAYS_BETWEEN = 14
REGULAR_WAVES = (1, 2, 3)
FINAL_WAVE = 4
FROM_EMAIL = "hm@relianmfg.com"
# No drafts are created on or after this UTC date (the show starts Nov 3).
FINAL_CUTOFF = date(2026, 11, 2)
# Local parts of helpdesk-style inboxes that are never mailed (matched after dropping . _ - +
# separators, and also against the first separator-delimited token, e.g. "support.eu").
HELPDESK_LOCALS = frozenset(
    {"support", "service", "customerservice", "help", "care", "noreply", "orders", "returns"}
)

# Waves whose template embeds a single approved banner inline via cid: (gmail_client's
# inline_image_path/inline_image_cid — the same mechanism used for combat_sports'
# hand_wraps_banner) instead of assembling the header from separate raw.githubusercontent.com
# <img> tags the way wave_1 does. The cid here must match the cid: referenced in the
# corresponding wave_N.html.
WAVE_INLINE_BANNERS = {
    2: (BRANDING_DIR / "eicma_wave2_banner.jpg", "eicma-wave2-banner"),
    3: (BRANDING_DIR / "eicma_wave3_banner.jpg", "eicma-wave3-banner"),
    # wave_4 reuses the approved wave_3 banner file under its own cid.
    4: (BRANDING_DIR / "eicma_wave3_banner.jpg", "eicma-wave4-banner"),
}

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


def load_wave(wave_number: int) -> tuple[str, str]:
    """Returns (html, subject) for an exact wave. No fallback — a missing wave is an error,
    not a reason to silently mail a different design."""
    html_path = WAVES_DIR / f"wave_{wave_number}.html"
    json_path = WAVES_DIR / f"wave_{wave_number}.json"
    if not (html_path.exists() and json_path.exists()):
        raise SystemExit(f"[eicma_campaign] wave_{wave_number} template missing in {WAVES_DIR}")
    subject = json.loads(json_path.read_text(encoding="utf-8"))["subject"]
    return html_path.read_text(encoding="utf-8"), subject


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    # Postgres emits 1-6 fractional digits; Python <3.11 fromisoformat only accepts 3 or 6.
    value = re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], value)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_helpdesk_email(email: str | None) -> bool:
    local = (email or "").strip().lower().split("@", 1)[0]
    if not local:
        return False
    first_token = re.split(r"[._+\-]", local, maxsplit=1)[0]
    return re.sub(r"[._+\-]", "", local) in HELPDESK_LOCALS or first_token in HELPDESK_LOCALS


def past_cutoff(now: datetime) -> bool:
    return now.astimezone(timezone.utc).date() >= FINAL_CUTOFF


def is_eligible(contact: dict, now: datetime, final: bool = False) -> bool:
    """The hard limits. Single source of truth for both the run and --report."""
    if past_cutoff(now):
        return False
    if contact.get("suppressed") or not (contact.get("email") or "").strip():
        return False
    if final and is_helpdesk_email(contact.get("email")):
        return False
    last_sent = _parse_ts(contact.get("last_sent_at"))
    if last_sent and now - last_sent < timedelta(days=MIN_DAYS_BETWEEN):
        return False
    if final:
        return contact.get("last_wave_design") != f"wave_{FINAL_WAVE}"
    return (contact.get("cycle_number") or 0) < MAX_CYCLES


def wave_for(contact: dict, final: bool) -> int:
    if final:
        return FINAL_WAVE
    return REGULAR_WAVES[min(contact.get("cycle_number") or 0, len(REGULAR_WAVES) - 1)]


def select_batch(contacts: list[dict], now: datetime, final: bool) -> tuple[list[dict], int]:
    """Returns (batch of at most DAILY_BATCH_SIZE, total eligible). Oldest last_sent_at first.
    In final mode an address that already has wave_4 on ANY row is excluded, and each address
    appears at most once in the batch."""
    already_final: set[str] = set()
    if final:
        already_final = {
            (c.get("email") or "").strip().lower()
            for c in contacts
            if c.get("last_wave_design") == f"wave_{FINAL_WAVE}"
        }
    eligible, seen = [], set()
    for c in contacts:
        email = (c.get("email") or "").strip().lower()
        if not is_eligible(c, now, final) or email in already_final or email in seen:
            continue
        seen.add(email)
        eligible.append(c)
    eligible.sort(key=lambda c: (c.get("last_sent_at") is not None, c.get("last_sent_at") or ""))
    return eligible[:DAILY_BATCH_SIZE], len(eligible)


def fetch_contacts(db) -> list[dict]:
    return db.table("eicma_invitations").select("*").limit(5000).execute().data


def report(contacts: list[dict], now: datetime) -> None:
    active = [c for c in contacts if not c.get("suppressed")]
    over_cap = [c for c in active if (c.get("cycle_number") or 0) >= MAX_CYCLES]
    print(f"[eicma_campaign] total={len(contacts)} suppressed={len(contacts) - len(active)} "
          f"non_suppressed={len(active)}")
    print(f"[eicma_campaign] non-suppressed at/over cap (cycle>={MAX_CYCLES}): {len(over_cap)}")
    print(f"[eicma_campaign] eligible for regular waves: "
          f"{sum(is_eligible(c, now) for c in contacts)}")
    print(f"[eicma_campaign] non-suppressed helpdesk-style inboxes (skipped in final mode): "
          f"{sum(is_helpdesk_email(c.get('email')) for c in active)}")
    print(f"[eicma_campaign] eligible for wave_{FINAL_WAVE} final reminder: "
          f"{select_batch(contacts, now, True)[1]}")
    if past_cutoff(now):
        print(f"[eicma_campaign] past cutoff {FINAL_CUTOFF} — nothing is eligible")


def run() -> None:
    final = os.environ.get("EICMA_FINAL_REMINDER") == "1"
    db = get_client()
    now = datetime.now(timezone.utc)
    all_contacts = fetch_contacts(db)

    if "--report" in sys.argv:
        report(all_contacts, now)
        return

    if past_cutoff(now):
        print(f"[eicma_campaign] on/after cutoff {FINAL_CUTOFF} — creating no drafts")
        return

    contacts, eligible_count = select_batch(all_contacts, now, final)
    print(f"[eicma_campaign] mode={'final' if final else 'regular'} "
          f"eligible={eligible_count} batch={len(contacts)}")

    templates: dict[int, tuple[str, str]] = {}

    for contact in contacts:
        wave = wave_for(contact, final)
        if wave not in templates:
            templates[wave] = load_wave(wave)
        html_template, subject = templates[wave]
        inline_banner_path, inline_banner_cid = WAVE_INLINE_BANNERS.get(wave, (None, None))

        greeting = extract_greeting(contact["contact_name"] or "")
        rendered_html = html_template.replace("{{GREETING}}", greeting)

        try:
            draft_id = create_draft(
                to_email=contact["email"],
                subject=subject,
                body=rendered_html,
                from_email=FROM_EMAIL,
                is_html=True,
                inline_image_path=inline_banner_path,
                inline_image_cid=inline_banner_cid or "banner-image",
            )
        except Exception as exc:  # noqa: BLE001 — one failed draft shouldn't block the rest
            print(f"[eicma_campaign] failed for {contact['email']}: {exc}")
            continue

        db.table("eicma_invitations").update(
            {
                "last_sent_at": datetime.now(timezone.utc).isoformat(),
                "status": "drafted",
                "last_wave_design": f"wave_{wave}",
                "cycle_number": (contact.get("cycle_number") or 0) + 1,
            }
        ).eq("id", contact["id"]).execute()
        print(f"[eicma_campaign] drafted {draft_id} for {contact['email']} "
              f"wave_{wave} greeting={greeting!r}")


if __name__ == "__main__":
    run()
