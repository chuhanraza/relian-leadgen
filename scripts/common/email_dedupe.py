"""Email-level duplicate and blocklist checks shared by the lead pipeline.

The old pipeline only de-duplicated by website domain, so the same inbox could be found
again under another brand name. These checks look at the email itself, across every vertical
and the EICMA invitation list, and also refuse customers / people told to be ignored.
"""
from __future__ import annotations

import json
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[2] / "config" / "never_email.json"
FREE_MAIL = ("gmail.", "googlemail.", "outlook.", "hotmail.", "live.", "yahoo.", "icloud.", "aol.", "msn.", "proton")


def _blocked_words() -> list[str]:
    return [w.lower() for w in json.loads(CONFIG.read_text()).get("domains", [])]


def is_free_mail(email: str) -> bool:
    domain = email.lower().rsplit("@", 1)[-1]
    return domain.startswith(FREE_MAIL)


def is_never_email(email: str) -> bool:
    domain = email.lower().rsplit("@", 1)[-1]
    labels = domain.replace("-", ".").split(".")
    # short names (dane, difi, jts) must be a whole word of the domain; longer ones can appear anywhere
    return any((w in domain) if len(w) >= 6 else (w in labels or w == domain) for w in _blocked_words())


def already_used(db, email: str, lead_id=None) -> bool:
    """True if this address is already on any lead (other than lead_id) or on the EICMA list."""
    e = email.strip().lower()
    q = db.table("outreach_leads").select("id").ilike("contact_email", e)
    for row in q.execute().data:
        if row["id"] != lead_id:
            return True
    return bool(db.table("eicma_invitations").select("id").ilike("email", e).limit(1).execute().data)


def reject_reason(db, lead: dict, email: str, vertical: str) -> str | None:
    if is_never_email(email):
        return "never_email_list"
    if already_used(db, email, lead.get("id")):
        return "duplicate_email"
    if vertical == "combat_sports" and is_free_mail(email):
        return "free_mail_address"
    return None
