"""Periodic seed-list ingestion (trade-show / directory exports) — NOT part of the daily
per-lead waterfall. Hamad supplies a CSV export from a trade-show exhibitor list (EICMA,
ISPO, Europages, etc.) — these change yearly and need a human to find/download the current
export, not something to auto-scrape blind. Expected columns: brand_name, region, email
(optional), website (optional). Inserts as vertical leads at status='researched', reusing
Lead Hunter's dedup-by-domain logic so re-running against an updated export is idempotent.
Rows with a real email are marked already-enriched (research_notes set, so Enricher's
`research_notes IS NULL` filter skips them); rows without one are left for Enricher to
research normally, same as any other lead, using the CSV's website column if provided.

Usage:
  python scripts/ingest_directory_list.py moto_apparel path/to/export.csv
  python scripts/ingest_directory_list.py combat_sports https://example.com/exhibitors.csv
"""

from __future__ import annotations

import csv
import io
import sys
import urllib.parse

import requests
from dotenv import load_dotenv

from common.db import get_client

load_dotenv()

REQUIRED_COLUMNS = {"brand_name", "region"}


def _load_rows(source: str) -> list[dict]:
    if source.startswith("http://") or source.startswith("https://"):
        resp = requests.get(source, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = resp.text
    else:
        with open(source, encoding="utf-8-sig") as f:
            text = f.read()
    reader = csv.DictReader(io.StringIO(text))
    if not REQUIRED_COLUMNS.issubset(set(reader.fieldnames or [])):
        sys.exit(f"[ingest_directory_list] CSV must include columns: {sorted(REQUIRED_COLUMNS)}")
    return list(reader)


def _domain_from(website: str | None, email: str | None) -> str | None:
    """Prefers the website column; falls back to the email's own domain (common in
    exhibitor lists that give an email but no separate site) — the check constraint on
    outreach_leads.domain is NOT NULL, so a row with neither is left out rather than
    inserted with a fabricated placeholder.
    """
    if website:
        url = website.strip()
        if not url.startswith("http"):
            url = f"https://{url}"
        netloc = urllib.parse.urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    if email and "@" in email:
        return email.strip().lower().split("@")[1]
    return None


def run(vertical: str, source: str) -> dict:
    db = get_client()
    rows = _load_rows(source)
    print(f"[ingest_directory_list] vertical={vertical} source={source} rows={len(rows)}")

    existing_domains = {
        row["domain"]
        for row in db.table("outreach_leads").select("domain").eq("vertical", vertical).execute().data
    }

    inserted = skipped_duplicate = skipped_no_domain = 0

    for row in rows:
        brand_name = (row.get("brand_name") or "").strip()
        region = (row.get("region") or "").strip()
        email = (row.get("email") or "").strip() or None
        website = (row.get("website") or "").strip() or None
        if not brand_name or not region:
            continue

        domain = _domain_from(website, email)
        if not domain:
            skipped_no_domain += 1
            print(
                f"[ingest_directory_list] skipped {brand_name!r}: no website or email to derive a domain from"
            )
            continue

        if domain in existing_domains:
            skipped_duplicate += 1
            continue
        existing_domains.add(domain)

        lead_row = {
            "vertical": vertical,
            "brand_name": brand_name,
            "domain": domain,
            "region": region,
            "website_url": website,
            "status": "researched",
        }
        if email and "@" in email:
            lead_row["contact_email"] = email
            lead_row["contact_method"] = "email"
            lead_row["research_notes"] = f"Provided by directory ingestion ({source})."

        db.table("outreach_leads").insert(lead_row).execute()
        inserted += 1
        print(f"[ingest_directory_list] inserted {brand_name!r} -> {domain}")

    result = {"inserted": inserted, "skipped_duplicate": skipped_duplicate, "skipped_no_domain": skipped_no_domain}
    print(f"[ingest_directory_list] done: {result}")
    return result


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("moto_apparel", "combat_sports"):
        sys.exit(
            "Usage: python scripts/ingest_directory_list.py <moto_apparel|combat_sports> <csv_path_or_url>"
        )
    run(sys.argv[1], sys.argv[2])
