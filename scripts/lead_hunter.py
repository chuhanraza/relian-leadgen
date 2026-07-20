"""Stage 1: Lead Hunter.

Picks the least-recently-covered region for the given vertical, runs a web-search-backed
discovery prompt, dedups against leadgen.outreach_leads by (vertical, domain), and inserts
new candidates as status='researched'.

Usage:
  python scripts/lead_hunter.py moto_apparel
  python scripts/lead_hunter.py combat_sports
"""

import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from common.gemini_client import research
from common.db import get_client
from common.parsing import extract_json
from common.regions import REGIONS_BY_VERTICAL

load_dotenv()

MOTO_PROMPT = """You are researching potential B2B outsourcing prospects for a technical
apparel manufacturer (Relian MFG, Sialkot, Pakistan) that makes motorcycle protective/
technical apparel (jackets, gloves, race suits) for other brands.

Find 5-8 boutique or mid-sized motorcycle technical apparel BRANDS based in or primarily
selling into: {region}.

Strict inclusion rule: only include companies whose own business model is "we are a brand
that designs/sells motorcycle apparel under our own label" — verify this from their own
site copy (About/Wholesale/Manufacturing pages), not just their product category.

Strict exclusion rule: exclude manufacturers, wholesale distributors, marketplaces/
retailers of other brands' gear, and companies whose own copy says they ARE the
manufacturer/factory/OEM for other brands.

For each qualifying brand, return: brand_name, domain (bare domain, e.g. example.com),
website_url, one_line_reasoning (why this passes the brand-not-manufacturer test, quoting
or paraphrasing their own site copy).

Reply with ONLY a fenced ```json code block containing a JSON array of objects with
exactly these keys: brand_name, domain, website_url, one_line_reasoning. No other text.
"""

COMBAT_PROMPT = """You are researching potential B2B outsourcing prospects for a
combat-sports gear manufacturer (Relian MFG, Sialkot, Pakistan) that makes boxing/MMA/
Muay Thai/BJJ gloves, wraps, and gear.

Find 5-8 combat-sports gyms, academies, shops/distributors, or small private gear brands
based in or primarily serving: {region}. Explicitly EXCLUDE anything based in Mainland
China. EXCLUDE general fitness/athletic wear companies and traditional non-combat dojos
(e.g. pure karate/taekwondo schools with no boxing/MMA/Muay Thai/BJJ program).

Classify each into exactly one sub_type:
- core_gym: a boxing/MMA/Muay Thai/BJJ gym or academy (potential branded-gear buyer)
- shop_distributor: a shop or distributor selling combat sports gear
- small_brand: a small private combat-sports gear brand

For each qualifying lead, return: brand_name, domain, website_url, sub_type,
one_line_reasoning.

Reply with ONLY a fenced ```json code block containing a JSON array of objects with
exactly these keys: brand_name, domain, website_url, sub_type, one_line_reasoning. No
other text.
"""

PROMPTS = {"moto_apparel": MOTO_PROMPT, "combat_sports": COMBAT_PROMPT}


def pick_next_region(db, vertical: str) -> str:
    regions = REGIONS_BY_VERTICAL[vertical]
    rows = (
        db.table("regions_covered")
        .select("*")
        .eq("vertical", vertical)
        .execute()
        .data
    )
    covered = {row["region"]: row for row in rows}

    uncovered = [r for r in regions if r not in covered]
    if uncovered:
        return uncovered[0]

    ranked = sorted(covered.values(), key=lambda row: row["last_searched_at"] or "")
    return ranked[0]["region"]


def run(vertical: str) -> int:
    db = get_client()
    region = pick_next_region(db, vertical)
    print(f"[lead_hunter] vertical={vertical} region={region}")

    prompt = PROMPTS[vertical].format(region=region)
    raw = research(prompt, max_uses=8)
    candidates = extract_json(raw)

    existing_domains = {
        row["domain"]
        for row in db.table("outreach_leads")
        .select("domain")
        .eq("vertical", vertical)
        .execute()
        .data
    }

    inserted = 0
    for candidate in candidates:
        domain = candidate.get("domain", "").strip().lower()
        if not domain or domain in existing_domains:
            continue
        existing_domains.add(domain)

        row = {
            "vertical": vertical,
            "brand_name": candidate.get("brand_name", "").strip(),
            "domain": domain,
            "region": region,
            "website_url": candidate.get("website_url"),
            "status": "researched",
        }
        if vertical == "combat_sports":
            sub_type = candidate.get("sub_type")
            if sub_type in ("core_gym", "shop_distributor", "small_brand"):
                row["sub_type"] = sub_type

        db.table("outreach_leads").insert(row).execute()
        inserted += 1

    now = datetime.now(timezone.utc).isoformat()
    db.table("regions_covered").upsert(
        {
            "vertical": vertical,
            "region": region,
            "last_searched_at": now,
            "leads_found_count": inserted,
        },
        on_conflict="vertical,region",
    ).execute()

    print(f"[lead_hunter] inserted {inserted} new leads for region={region}")
    return inserted


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in PROMPTS:
        sys.exit("Usage: python scripts/lead_hunter.py <moto_apparel|combat_sports>")
    run(sys.argv[1])
