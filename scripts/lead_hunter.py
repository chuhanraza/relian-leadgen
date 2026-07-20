"""Stage 1: Lead Hunter.

Picks the least-recently-covered region for the given vertical, then runs a two-step
search: (1) discover candidate brand/gym NAMES from a broad search — this mostly surfaces
"best of" listicle articles rather than the brands' own sites, so it only extracts names,
never guesses a domain from listicle content; (2) for each name, a separate targeted
search resolves their actual official domain and re-verifies the brand-vs-manufacturer /
sub_type classification against that domain's own site content. Dedups against
leadgen.outreach_leads by (vertical, domain), inserts new candidates as status='researched'.

Usage:
  python scripts/lead_hunter.py moto_apparel
  python scripts/lead_hunter.py combat_sports
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from common.db import get_client
from common.groq_client import generate
from common.parsing import extract_json
from common.regions import REGIONS_BY_VERTICAL
from common.web_search import (
    ddg_search,
    fetch_page_text,
    format_results,
    get_ddg_failure_count,
    reset_ddg_failure_count,
)

load_dotenv()

MAX_REGIONS_PER_RUN = 4
QUALIFIED_TARGET_PER_RUN = 20

DISCOVERY_QUERIES = {
    "moto_apparel": "boutique motorcycle technical apparel brand {region}",
    "combat_sports": "boxing MMA Muay Thai BJJ gym academy shop brand {region}",
}

DISCOVERY_PROMPTS = {
    "moto_apparel": """Web search results for "boutique motorcycle apparel brands {region}":
{search_results}

From the text above (likely includes "best of" roundup articles), extract up to 10
DISTINCT NAMES of boutique or mid-sized motorcycle technical apparel BRANDS (own-label
companies, not manufacturers/distributors/marketplaces) that are based in or sell into:
{region}. Just the names — do not guess a domain or URL at this stage.

Reply with ONLY a fenced ```json code block: a JSON array of objects with exactly this
key: brand_name. No other text. Empty array if nothing qualifies.
""",
    "combat_sports": """Web search results for "boxing MMA Muay Thai BJJ gym/shop/brand {region}":
{search_results}

From the text above (likely includes "best of" roundup articles), extract up to 10
DISTINCT NAMES of combat-sports gyms, academies, shops/distributors, or small private gear
brands based in or primarily serving: {region}. Explicitly EXCLUDE anything you can tell is
based in Mainland China, general fitness/athletic wear companies, and traditional
non-combat dojos (pure karate/taekwondo with no boxing/MMA/Muay Thai/BJJ program). Just the
names — do not guess a domain or URL at this stage.

Reply with ONLY a fenced ```json code block: a JSON array of objects with exactly this
key: brand_name. No other text. Empty array if nothing qualifies.
""",
}

VERIFY_PROMPTS = {
    "moto_apparel": """Candidate: "{brand_name}" (a possible motorcycle apparel brand, region: {region})

Search results for their official site:
{search_results}

Homepage text of the top likely match (may be empty if unfetchable):
{page_text}

1. Identify which result (if any) is their actual official website — not a marketplace,
   review site, social media aggregator, or unrelated company. If none of the results look
   like their real official site, say so.
2. Using the homepage text if available, verify: is this a BRAND (designs/sells apparel
   under its own label) rather than a manufacturer/wholesale distributor/OEM factory? If
   the homepage text says they ARE the manufacturer/factory for other brands, this fails.
3. Confirm this is an INDEPENDENT, BOUTIQUE-TO-MID-SIZE operation, not a
   good private-label manufacturing prospect if it's actually one of
   these: a globally recognized heritage or luxury brand, a brand owned
   by or part of a large corporate/fashion group, or a brand with
   mass-market distribution across major retail chains (e.g. brands at
   the scale of Belstaff, Alpinestars, Dainese are TOO LARGE —
   disqualify regardless of how steps 1-2 went). If you recognize the
   name as large/well-established/conglomerate-owned from your own
   knowledge, or the homepage text itself signals large scale (e.g.
   investor-relations language, "since 18xx" heritage branding, dozens
   of international retail locations), this fails.

Reply with ONLY a fenced ```json code block, a single object with exactly these keys:
- qualifies: boolean (true only if you found their real official site, it's a brand not a
  manufacturer/distributor, AND it passes the independent boutique/mid-size check in step 3)
- domain: bare domain (e.g. example.com) of their official site, or null
- website_url: full URL, or null
- one_line_reasoning: string (state explicitly if it failed step 3 specifically)
No other text.
""",
    "combat_sports": """Candidate: "{brand_name}" (a possible combat-sports gym/shop/brand, region: {region})

Search results for their official site:
{search_results}

Homepage text of the top likely match (may be empty if unfetchable):
{page_text}

1. Identify which result (if any) is their actual official website — not a marketplace,
   review site, or unrelated company. If none of the results look like their real official
   site, say so.
2. Using the homepage text if available, confirm this is genuinely combat-sports related
   (boxing/MMA/Muay Thai/BJJ), not general fitness or a traditional non-combat dojo, and
   not based in Mainland China.
3. Confirm this is NOT a large corporate-owned gym franchise/chain
   (national or international), and NOT a big-box retail chain
   masquerading as a "shop" — if so, disqualify regardless of category
   fit, even if it otherwise looks like a good match.
4. Classify into exactly one sub_type: core_gym (a gym/academy), shop_distributor (a shop
   or distributor), or small_brand (a small private gear brand).

Reply with ONLY a fenced ```json code block, a single object with exactly these keys:
- qualifies: boolean (false if step 3 fails, regardless of other steps)
- domain: bare domain (e.g. example.com), or null
- website_url: full URL, or null
- sub_type: one of "core_gym", "shop_distributor", "small_brand", or null
- one_line_reasoning: string
No other text.
""",
}


def pick_regions(db, vertical: str, n: int) -> list[str]:
    """Up to n regions, least-recently-searched first (never-searched regions come
    before any region that's ever been searched, then oldest last_searched_at). All n
    are picked up front in one ranking pass, so there's no risk of a region repeating
    within the same run even though several get processed sequentially.
    """
    regions = REGIONS_BY_VERTICAL[vertical]
    rows = db.table("regions_covered").select("*").eq("vertical", vertical).execute().data
    covered = {row["region"]: row for row in rows}

    uncovered = [r for r in regions if r not in covered]
    ranked_covered = sorted(covered.values(), key=lambda row: row["last_searched_at"] or "")
    ordered = uncovered + [row["region"] for row in ranked_covered]
    return ordered[:n]


def discover_names(vertical: str, region: str) -> list[str]:
    query = DISCOVERY_QUERIES[vertical].format(region=region)
    results = ddg_search(query, max_results=10)
    prompt = DISCOVERY_PROMPTS[vertical].format(region=region, search_results=format_results(results))
    raw = generate(prompt, max_tokens=1024)
    candidates = extract_json(raw)
    names = [c.get("brand_name", "").strip() for c in candidates if c.get("brand_name")]
    return names[:15]


def _name_match_score(brand_name: str, url: str) -> int:
    """How many normalized brand-name tokens appear in the URL's domain — used to pick
    the result that's actually THIS brand, not an unrelated same-ish-named company
    (e.g. "WarForged Apparel" vs an unrelated UK brand also called "Warforged").
    """
    domain = url.lower()
    tokens = re.findall(r"[a-z0-9]+", brand_name.lower())
    return sum(1 for t in tokens if len(t) > 2 and t in domain)


def verify_candidate(vertical: str, brand_name: str, region: str) -> dict | None:
    results = ddg_search(f'"{brand_name}" official website', max_results=4)
    ranked = sorted(results, key=lambda r: _name_match_score(brand_name, r["url"]), reverse=True)
    page_text = ""
    for r in ranked:
        page_text = fetch_page_text(r["url"])
        if page_text:
            break

    prompt = VERIFY_PROMPTS[vertical].format(
        brand_name=brand_name,
        region=region,
        search_results=format_results(ranked),
        page_text=page_text or "(could not fetch a page)",
    )
    try:
        raw = generate(prompt, max_tokens=512)
        data = extract_json(raw)
    except Exception as exc:  # noqa: BLE001 — one candidate's failure shouldn't kill the run
        print(f"[lead_hunter] verify failed for {brand_name!r}: {exc}")
        return None

    if not data.get("qualifies") or not data.get("domain"):
        return None
    return data


def run(vertical: str) -> dict:
    reset_ddg_failure_count()
    db = get_client()
    regions = pick_regions(db, vertical, MAX_REGIONS_PER_RUN)
    print(f"[lead_hunter] vertical={vertical} regions={regions}")

    existing_domains = {
        row["domain"]
        for row in db.table("outreach_leads").select("domain").eq("vertical", vertical).execute().data
    }

    regions_processed: list[str] = []
    total_inserted = 0

    for region in regions:
        print(f"[lead_hunter] --- region={region} ---")
        failures_before = get_ddg_failure_count()

        names = discover_names(vertical, region)
        print(f"[lead_hunter] discovered {len(names)} candidate names: {names}")

        inserted_this_region = 0
        for brand_name in names:
            verified = verify_candidate(vertical, brand_name, region)
            if not verified:
                continue

            domain = verified["domain"].strip().lower()
            if not domain or domain in existing_domains:
                continue
            existing_domains.add(domain)

            row = {
                "vertical": vertical,
                "brand_name": brand_name,
                "domain": domain,
                "region": region,
                "website_url": verified.get("website_url"),
                "status": "researched",
            }
            if vertical == "combat_sports":
                sub_type = verified.get("sub_type")
                if sub_type in ("core_gym", "shop_distributor", "small_brand"):
                    row["sub_type"] = sub_type

            db.table("outreach_leads").insert(row).execute()
            inserted_this_region += 1
            total_inserted += 1
            print(f"[lead_hunter] verified + inserted {brand_name!r} -> {domain}")

        now = datetime.now(timezone.utc).isoformat()
        db.table("regions_covered").upsert(
            {
                "vertical": vertical,
                "region": region,
                "last_searched_at": now,
                "leads_found_count": inserted_this_region,
                "ddg_failures": get_ddg_failure_count() - failures_before,
            },
            on_conflict="vertical,region",
        ).execute()
        regions_processed.append(region)

        print(
            f"[lead_hunter] region={region} inserted={inserted_this_region} "
            f"(run total={total_inserted})"
        )

        if total_inserted >= QUALIFIED_TARGET_PER_RUN:
            print(
                f"[lead_hunter] reached {QUALIFIED_TARGET_PER_RUN} qualified leads this "
                f"run, stopping early ({len(regions_processed)}/{len(regions)} regions processed)"
            )
            break

    result = {
        "regions_processed": regions_processed,
        "leads_inserted": total_inserted,
        "ddg_failures": get_ddg_failure_count(),
    }
    print(f"[lead_hunter] run complete: {result}")
    return result


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in DISCOVERY_QUERIES:
        sys.exit("Usage: python scripts/lead_hunter.py <moto_apparel|combat_sports>")
    run(sys.argv[1])
