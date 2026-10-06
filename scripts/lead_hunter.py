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

import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv

from common.db import get_client
from common.groq_client import (
    MODEL_FAST,
    MODEL_QUALITY,
    generate,
    get_call_count,
    get_tokens_used,
    reset_call_count,
    reset_tokens_used,
)
from common.token_budget import stage_budget
from common.token_usage_log import record as record_tokens
from common.parsing import extract_json
from common.regions import REGIONS_BY_VERTICAL
from common.web_search import (
    ddg_search,
    fetch_page_text,
    format_results,
    get_ddg_failure_count,
    get_ddg_retry_saves,
    reset_ddg_failure_count,
)

load_dotenv()

MAX_REGIONS_PER_RUN = 2
QUALIFIED_TARGET_PER_RUN = 20

# combat_sports only: about half of qualified leads never yield a findable email
# (confirmed in production data), so Lead Hunter targets a raw-qualified buffer well
# above the ~50/day drafted goal, and keeps working across full region passes each run
# until that buffer is met for the day rather than stopping at a fixed per-run count.
DAILY_RAW_TARGET = 110
MAX_DAILY_PASSES = 3

# Token caps are per stage and per model (common/token_budget.py): 90% of each model's free
# 200K/day, split over the 4 daily runs, so the lead hunter can never starve the Enricher /
# Copywriter that follow it in the same job, or the day's later runs. Discovery runs on
# gpt-oss-120b, verify on gpt-oss-20b, so neither model's quota is exhausted by this stage.
DISCOVER_TOKEN_BUDGET = stage_budget("discover")  # on MODEL_QUALITY
VERIFY_TOKEN_BUDGET = stage_budget("verify")  # on MODEL_FAST

# Verify prompt size controls (target <= ~1,000 tokens per verify call, prompt + reply).
VERIFY_MAX_RESULTS = 3
VERIFY_SNIPPET_CHARS = 200
VERIFY_PAGE_CHARS = 1200

DISCOVERY_QUERIES = {
    "moto_apparel": "boutique motorcycle technical apparel brand {region}",
    "combat_sports": "boxing MMA Muay Thai BJJ gym academy shop brand {region}",
}

# Re-passes over the full region list (combat_sports only) vary the discovery query's
# phrasing instead of repeating the exact same search DuckDuckGo already answered —
# pass 0 is the original phrasing so a single-pass run behaves identically to before.
DISCOVERY_QUERY_VARIANTS = {
    "moto_apparel": [
        DISCOVERY_QUERIES["moto_apparel"],
        "independent motorcycle riding jacket pants gear label {region}",
        "small batch motorcycle textile leather riding wear brand {region}",
    ],
    "combat_sports": [
        "boxing MMA Muay Thai BJJ gym academy shop brand {region}",
        "independent boxing Muay Thai MMA BJJ gym or gear brand {region}",
        "combat sports training academy small shop or brand {region}",
    ],
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
    "moto_apparel": """Candidate: "{brand_name}" (possible motorcycle apparel brand, region: {region})

Search results (title | url | snippet):
{search_results}

Homepage text (may be empty):
{page_text}

1. Which result, if any, is their OFFICIAL website (not a marketplace, review site or
   unrelated company)? If none, a result that is clearly their own business Facebook Page
   counts: domain "facebook.com/<pagename>", website_url the full URL, and judge from
   snippets alone. Otherwise no domain.
2. Is it a BRAND selling apparel under its own label, rather than a manufacturer,
   wholesale distributor or OEM factory for other brands?

Reply ONLY with a fenced ```json block, one object with exactly these keys:
qualifies (bool: official site/page found AND it is a brand), domain (bare domain or null),
website_url (or null), one_line_reasoning (string).
""",
    "combat_sports": """Candidate: "{brand_name}" (possible combat-sports gym/shop/brand, region: {region})

Search results (title | url | snippet):
{search_results}

Homepage text (may be empty):
{page_text}

1. Which result, if any, is their OFFICIAL website (not a marketplace, review site or
   unrelated company)? If none, a result that is clearly their own business Facebook Page
   counts: domain "facebook.com/<pagename>", website_url the full URL, and judge from
   snippets alone. Otherwise no domain.
2. Is it genuinely combat-sports (boxing/MMA/Muay Thai/BJJ), not general fitness or a
   traditional non-combat dojo, and not based in Mainland China?
3. sub_type: core_gym (gym/academy), shop_distributor (shop or distributor) or small_brand
   (small private gear brand).

Reply ONLY with a fenced ```json block, one object with exactly these keys:
qualifies (bool), domain (bare domain or null), website_url (or null),
sub_type ("core_gym"|"shop_distributor"|"small_brand"|null), one_line_reasoning (string).
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


def discover_names(vertical: str, region: str, pass_index: int = 0) -> list[str]:
    variants = DISCOVERY_QUERY_VARIANTS[vertical]
    query = variants[pass_index % len(variants)].format(region=region)
    results = ddg_search(query, max_results=8)
    prompt = DISCOVERY_PROMPTS[vertical].format(
        region=region, search_results=format_results(results, 220)
    )
    try:
        raw = generate(prompt, max_tokens=700, model=MODEL_QUALITY)
        candidates = extract_json(raw)
    except Exception as exc:  # noqa: BLE001 — e.g. Groq rate limit; one region's failure
        # shouldn't crash the whole run and skip every remaining region plus later stages.
        print(f"[lead_hunter] discovery failed for region={region!r}: {exc}")
        return []
    # The fast model occasionally replies with a JSON array of bare strings instead of
    # the requested {"brand_name": ...} objects — skip anything that isn't the expected
    # shape rather than crashing the whole run on a single malformed response.
    names = []
    for c in candidates:
        if isinstance(c, dict):
            name = c.get("brand_name", "").strip()
        elif isinstance(c, str):
            name = c.strip()
        else:
            continue
        if name:
            names.append(name)
    return names[:10]


def _name_match_score(brand_name: str, url: str) -> int:
    """How many normalized brand-name tokens appear in the URL's domain — used to pick
    the result that's actually THIS brand, not an unrelated same-ish-named company
    (e.g. "WarForged Apparel" vs an unrelated UK brand also called "Warforged").
    """
    domain = url.lower()
    tokens = re.findall(r"[a-z0-9]+", brand_name.lower())
    return sum(1 for t in tokens if len(t) > 2 and t in domain)


_DENYLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "domain_denylist.json"
# Per-run counters of why candidates were dropped, printed at the end of run().
VERIFY_STATS: dict[str, int] = {}


def _bump(reason: str) -> None:
    VERIFY_STATS[reason] = VERIFY_STATS.get(reason, 0) + 1


def _norm_domain(url_or_domain: str) -> str:
    """'https://www.Venum.com/x?y' -> 'venum.com'. facebook.com keeps its first path
    segment ('facebook.com/venumasia') since the page, not the host, identifies the lead.
    """
    d = re.sub(r"^https?://", "", (url_or_domain or "").strip().lower())
    d = re.sub(r"^www\.", "", d.split("?")[0].split("#")[0]).rstrip("/")
    host, _, path = d.partition("/")
    if host == "facebook.com" and path:
        return f"facebook.com/{path.split('/')[0]}"
    return host


def _load_denylist() -> tuple[set[str], tuple[str, ...]]:
    try:
        cfg = json.loads(_DENYLIST_PATH.read_text())
    except Exception as exc:  # noqa: BLE001 — a missing file must not stop the pipeline
        print(f"[lead_hunter] could not load {_DENYLIST_PATH}: {exc}")
        return set(), ()
    return {_norm_domain(d) for d in cfg.get("domains", [])}, tuple(cfg.get("suffixes", []))


_DENY_DOMAINS, _DENY_SUFFIXES = _load_denylist()


def is_denylisted(url_or_domain: str) -> bool:
    d = _norm_domain(url_or_domain)
    if d in _DENY_DOMAINS or any("/" not in x and d.endswith("." + x) for x in _DENY_DOMAINS):
        return True
    return any(d == suf or d.endswith("." + suf) for suf in _DENY_SUFFIXES)


def _brand_is_denylisted(brand_name: str) -> str | None:
    """Name-level check, before any search: the brand's normalized name is a prefix of (or
    prefixed by) the second-level label of a denylisted domain, e.g. 'Vuori' vs
    vuoriclothing.com, 'Venum Asia' vs venum.com. Returns the matching denylist domain.
    """
    norm = re.sub(r"[^a-z0-9]", "", brand_name.lower())
    if len(norm) < 5:
        return None
    for d in _DENY_DOMAINS:
        host = d.split("/")[0]
        label = re.sub(r"[^a-z0-9]", "", host.split(".")[0])
        if len(label) >= 5 and (norm.startswith(label) or label.startswith(norm)):
            return d
    return None


def verify_candidate(
    vertical: str, brand_name: str, region: str, existing_domains: set[str] | None = None
) -> dict | None:
    # --- deterministic checks first: zero tokens spent -----------------------------
    if hit := _brand_is_denylisted(brand_name):
        print(f"[lead_hunter] denylist name match for {brand_name!r} ~ {hit}, skipped (0 tokens, 0 searches)")
        _bump("denylist")
        return None
    results = ddg_search(f'"{brand_name}" official website', max_results=VERIFY_MAX_RESULTS)
    if not results:
        # Nothing to verify against (a DDG failure); the model would just be guessing from
        # its own memory, so don't pay for the call.
        _bump("no_search_results")
        return None
    ranked = sorted(results, key=lambda r: _name_match_score(brand_name, r["url"]), reverse=True)
    # Fail closed: any result that looks like this brand (name token in its URL) OR the
    # top-ranked result being denylisted rejects the candidate.
    related = [r for r in ranked if _name_match_score(brand_name, r["url"]) > 0] + ranked[:1]
    if hit := next((r["url"] for r in related if is_denylisted(r["url"])), None):
        print(f"[lead_hunter] denylist hit for {brand_name!r} -> {_norm_domain(hit)}, skipped (0 tokens)")
        _bump("denylist")
        return None
    if existing_domains and _norm_domain(ranked[0]["url"]) in {_norm_domain(d) for d in existing_domains}:
        _bump("already_known")
        return None

    page_text = ""
    for r in ranked:
        if "facebook.com" in r["url"].lower():
            continue  # login wall — never fetch, the model relies on the snippet text instead
        page_text = fetch_page_text(r["url"], max_chars=VERIFY_PAGE_CHARS)
        if page_text:
            break

    prompt = VERIFY_PROMPTS[vertical].format(
        brand_name=brand_name,
        region=region,
        search_results=format_results(ranked[:VERIFY_MAX_RESULTS], VERIFY_SNIPPET_CHARS),
        page_text=page_text or "(could not fetch a page)",
    )
    # Single pass, MODEL_FAST only.
    try:
        raw = generate(prompt, max_tokens=400, model=MODEL_FAST)
        data = extract_json(raw)
    except Exception as exc:  # noqa: BLE001 — one candidate's failure shouldn't kill the run
        print(f"[lead_hunter] verify failed for {brand_name!r}: {exc}")
        _bump("llm_error")
        return None

    domain = data.get("domain")
    # The model occasionally emits the literal string "null"/"none" instead of a real
    # JSON null when it means "no domain found" — treat those the same as missing.
    if not data.get("qualifies") or not domain or str(domain).strip().lower() in ("null", "none"):
        _bump("rejected_by_model")
        return None
    if is_denylisted(str(domain)):
        _bump("denylist")
        return None
    _bump("accepted")
    return data


def _process_region(
    db, vertical: str, region: str, existing_domains: set[str], pass_index: int = 0
) -> int:
    """Runs discovery + verify + insert for one region. Returns count newly inserted
    (mutates existing_domains as it goes so later regions/passes in the same run see
    dupes from earlier ones immediately). Always upserts regions_covered so the next
    pick_regions() call reflects this region as just-searched, regardless of vertical
    or which run() path called it.
    """
    print(f"[lead_hunter] --- region={region} (pass={pass_index}) ---")
    failures_before = get_ddg_failure_count()

    names = discover_names(vertical, region, pass_index=pass_index)
    print(f"[lead_hunter] discovered {len(names)} candidate names: {names}")

    inserted_this_region = 0
    for brand_name in names:
        verified = verify_candidate(vertical, brand_name, region, existing_domains)
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

    print(f"[lead_hunter] region={region} inserted={inserted_this_region}")
    return inserted_this_region


def _today_start_utc_iso() -> str:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()


def _count_today(db, vertical: str) -> int:
    resp = (
        db.table("outreach_leads")
        .select("id", count="exact")
        .eq("vertical", vertical)
        .gte("created_at", _today_start_utc_iso())
        .execute()
    )
    return resp.count or 0


def _budget_hit() -> bool:
    """Either stage's per-run token budget is spent (discovery on 120B, verify on 20B)."""
    return (
        get_tokens_used(MODEL_QUALITY) >= DISCOVER_TOKEN_BUDGET
        or get_tokens_used(MODEL_FAST) >= VERIFY_TOKEN_BUDGET
    )


def _run_fixed(vertical: str) -> dict:
    """Original behavior, unchanged: up to MAX_REGIONS_PER_RUN regions, stop once
    QUALIFIED_TARGET_PER_RUN qualified leads are inserted this run. Still used by
    moto_apparel.
    """
    reset_ddg_failure_count()
    reset_call_count()
    reset_tokens_used()
    db = get_client()
    regions = pick_regions(db, vertical, MAX_REGIONS_PER_RUN)
    # Rotate the discovery phrasing by day so repeat visits to a region don't re-ask DDG
    # the exact same question.
    variant_index = datetime.now(timezone.utc).toordinal() % len(DISCOVERY_QUERY_VARIANTS[vertical])
    print(f"[lead_hunter] vertical={vertical} regions={regions}")

    existing_domains = {
        row["domain"]
        for row in db.table("outreach_leads").select("domain").eq("vertical", vertical).execute().data
    }

    regions_processed: list[str] = []
    total_inserted = 0

    for region in regions:
        if _budget_hit():
            print(f"[lead_hunter] Groq token budget reached, stopping early (calls={get_call_count()})")
            break
        inserted_this_region = _process_region(
            db, vertical, region, existing_domains, pass_index=variant_index
        )
        total_inserted += inserted_this_region
        regions_processed.append(region)

        print(f"[lead_hunter] (run total={total_inserted})")

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
        "groq_calls": get_call_count(),
        "groq_tokens_used": get_tokens_used(),
    }
    # moto_apparel never recorded its lead_hunter usage before, so daily_run_log undercounted it.
    record_tokens("lead_hunter", get_tokens_used(), get_call_count())
    print(f"[lead_hunter] verify_stats={VERIFY_STATS} ddg_retry_saves={get_ddg_retry_saves()}")
    print(f"[lead_hunter] run complete: {result}")
    return result


def _run_daily_target(vertical: str) -> dict:
    """combat_sports only: keeps working until DAILY_RAW_TARGET raw-qualified leads
    exist for today (UTC), re-checked at the start of every run so the 4x/day cron
    naturally tops up whatever's still short rather than assuming a fresh day each
    time. Loops full region passes (varying discovery query phrasing after the first)
    until the remaining target is met, or MAX_DAILY_PASSES passes complete with the
    last one finding zero new qualifying candidates — the real "supply exhausted for
    today" signal, logged explicitly rather than just quietly stopping.
    """
    reset_ddg_failure_count()
    reset_call_count()
    reset_tokens_used()
    db = get_client()

    today_count_before = _count_today(db, vertical)
    remaining = DAILY_RAW_TARGET - today_count_before
    print(
        f"[lead_hunter] vertical={vertical} daily_target={DAILY_RAW_TARGET} "
        f"today_count_before={today_count_before} remaining={remaining}"
    )

    if remaining <= 0:
        print("[lead_hunter] today's target already met, exiting cleanly without searching")
        record_tokens("lead_hunter", get_tokens_used(), get_call_count())
        return {
            "regions_processed": [],
            "leads_inserted": 0,
            "today_count_before": today_count_before,
            "today_count_after": today_count_before,
            "passes_run": 0,
            "outcome": "already_met",
            "ddg_failures": 0,
        }

    regions = REGIONS_BY_VERTICAL[vertical]
    existing_domains = {
        row["domain"]
        for row in db.table("outreach_leads").select("domain").eq("vertical", vertical).execute().data
    }

    regions_processed: list[str] = []
    total_inserted = 0
    outcome = "target_met"
    passes_run = 0

    for pass_index in range(MAX_DAILY_PASSES):
        passes_run = pass_index + 1
        print(f"[lead_hunter] === pass {passes_run}/{MAX_DAILY_PASSES} (remaining={remaining - total_inserted}) ===")

        # Full pick_regions ranking each pass so the least-recently-searched regions
        # (including ones just covered earlier in this same run) are still ordered
        # sensibly — but since n covers the whole list, every region is processed
        # regardless of order.
        ranked_regions = pick_regions(db, vertical, len(regions))
        pass_inserted = 0
        groq_budget_hit = False

        for region in ranked_regions:
            if _budget_hit():
                groq_budget_hit = True
                print(
                    f"[lead_hunter] Groq token budget reached (discover={get_tokens_used(MODEL_QUALITY)}/"
                    f"{DISCOVER_TOKEN_BUDGET}, verify={get_tokens_used(MODEL_FAST)}/{VERIFY_TOKEN_BUDGET}, "
                    f"calls={get_call_count()}) — stopping early "
                    f"this run to leave headroom for Enricher/Copywriter later in this job and "
                    f"later runs today (total_inserted={total_inserted}, remaining={remaining})"
                )
                break

            inserted_this_region = _process_region(
                db, vertical, region, existing_domains, pass_index=pass_index
            )
            pass_inserted += inserted_this_region
            total_inserted += inserted_this_region
            if region not in regions_processed:
                regions_processed.append(region)

            print(
                f"[lead_hunter] (pass total={pass_inserted}, run total={total_inserted}, "
                f"groq_calls={get_call_count()}, groq_tokens_used={get_tokens_used()})"
            )

            if total_inserted >= remaining:
                break

        if total_inserted >= remaining:
            print(
                f"[lead_hunter] remaining target satisfied mid-pass "
                f"(pass {passes_run}, total_inserted={total_inserted} >= remaining={remaining})"
            )
            outcome = "target_met"
            break

        if groq_budget_hit:
            outcome = "groq_budget_reached"
            break

        if passes_run == MAX_DAILY_PASSES:
            if pass_inserted == 0:
                outcome = "exhausted"
                print(
                    f"[lead_hunter] today's supply genuinely exhausted: {MAX_DAILY_PASSES} full "
                    f"passes complete, zero new qualifying candidates found in the last pass "
                    f"(total_inserted={total_inserted}, still short of remaining={remaining})"
                )
            else:
                outcome = "max_passes_reached"
                print(
                    f"[lead_hunter] hit {MAX_DAILY_PASSES}-pass cap still short of target "
                    f"(total_inserted={total_inserted}, remaining={remaining}) — last pass still "
                    f"found {pass_inserted} new, so not calling this exhaustion; next cron run "
                    f"will top up further"
                )
            break

        print(
            f"[lead_hunter] pass {passes_run} complete, found {pass_inserted} new this pass, "
            f"still short of remaining={remaining} (total_inserted={total_inserted}) — starting another pass"
        )

    today_count_after = today_count_before + total_inserted
    result = {
        "regions_processed": regions_processed,
        "leads_inserted": total_inserted,
        "today_count_before": today_count_before,
        "today_count_after": today_count_after,
        "passes_run": passes_run,
        "outcome": outcome,
        "ddg_failures": get_ddg_failure_count(),
        "groq_calls": get_call_count(),
        "groq_tokens_used": get_tokens_used(),
    }
    record_tokens("lead_hunter", get_tokens_used(), get_call_count())
    print(f"[lead_hunter] verify_stats={VERIFY_STATS} ddg_retry_saves={get_ddg_retry_saves()}")
    print(f"[lead_hunter] run complete: {result}")
    return result


def run(vertical: str) -> dict:
    if vertical == "combat_sports":
        return _run_daily_target(vertical)
    return _run_fixed(vertical)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in DISCOVERY_QUERIES:
        sys.exit("Usage: python scripts/lead_hunter.py <moto_apparel|combat_sports>")
    run(sys.argv[1])
