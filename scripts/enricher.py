"""Stage 2: Enricher.

For every status='researched' lead that hasn't been enriched yet (research_notes IS NULL),
fetches the brand's actual site + a DuckDuckGo search for their contact info, pulls 1-2
genuine specific details, and attempts to find a real published contact email. If that
primary route finds nothing, it next crawls the lead's OWN site more thoroughly
(common.contact_discovery.crawl_domain_secondary_pages — sitemap.xml / /contact / /about
pages), since that's re-checking an already-known domain rather than trying a new source.
Only if that also finds nothing does it fall back to common.contact_discovery's waterfall
(Overpass -> Facebook dork -> Instagram/Linktree -> Apollo.io, verified emails only, capped
by APOLLO_MAX_CALLS_PER_RUN) before giving up. Never guesses an email pattern — no email
found anywhere means status='skipped_no_email' and the lead does not proceed to the
Copywriter.

Usage:
  python scripts/enricher.py moto_apparel
  python scripts/enricher.py combat_sports
"""

import os
import sys

from dotenv import load_dotenv

from common.contact_discovery import (
    EMAIL_RE,
    crawl_domain_secondary_pages,
    discover_contact,
    email_acceptable,
    get_last_apollo_outcome,
    is_generic_role_email,
    reset_apollo_call_count,
)
from common.db import get_client
from common.groq_client import MODEL_FAST, generate, get_call_count, get_tokens_used, reset_tokens_used
from common.lead_scoring import score_lead
from common.parsing import extract_json
from common.token_usage_log import record as record_tokens
from common.web_search import ddg_search, fetch_page_html, format_results, html_to_text

load_dotenv()

APOLLO_MAX_CALLS_PER_RUN = int(os.environ.get("APOLLO_MAX_CALLS_PER_RUN") or "5")

# Per-run cap so Enricher makes steady, safe progress every run instead of trying the
# entire pending backlog (each lead involves several real network calls) in one
# sequential pass — safely under what the 4x/day cron can clear across a day.
ENRICHER_BATCH_SIZE = 40

PROMPT = """Research this company for a B2B outreach pitch: {brand_name} ({website_url}).

Homepage text (may be empty if the page couldn't be fetched):
{page_text}

Web search results for "{brand_name} contact email":
{search_results}

1. From the material above, find 1-2 GENUINE, specific details — positioning, materials
   they currently use, or any stated values like "made domestically"/"made in USA"/
   "handmade locally". These must be real specifics actually present above, not generic
   guesses. If the brand markets itself as domestically-made or in-house manufactured,
   note that explicitly — it makes them a weaker outsourcing prospect, so flag it rather
   than omitting it.

2. From the material above, find a REAL, PUBLISHED contact email. Do NOT guess or
   construct a pattern email (e.g. do not invent info@domain.com unless it actually
   appears in the text above). If no email appears above, say so — do not fabricate.

Reply with ONLY a fenced ```json code block, a single object with exactly these keys:
- research_notes: string, 1-3 sentences with the specific detail(s) found
- domestically_made: boolean, true if they market themselves as domestic/in-house made
- contact_email: string with the real published email, or null if none was found
No other text.
"""


def run(vertical: str) -> None:
    reset_apollo_call_count()
    reset_tokens_used()
    db = get_client()
    leads = (
        db.table("outreach_leads")
        .select("*")
        .eq("vertical", vertical)
        .eq("status", "researched")
        .is_("research_notes", "null")
        .order("created_at")
        .limit(ENRICHER_BATCH_SIZE)
        .execute()
        .data
    )
    print(f"[enricher] vertical={vertical} pending_this_batch={len(leads)} (cap={ENRICHER_BATCH_SIZE})")

    for lead in leads:
        website_url = lead.get("website_url") or f"https://{lead['domain']}"
        html = fetch_page_html(website_url)
        page_text = html_to_text(html, max_chars=4000) if html else "(could not fetch page)"
        lead_score = score_lead(html)
        search_results = format_results(
            ddg_search(f"{lead['brand_name']} contact email", max_results=5)
        )
        prompt = PROMPT.format(
            brand_name=lead["brand_name"],
            website_url=website_url,
            page_text=page_text,
            search_results=search_results,
        )
        try:
            raw = generate(prompt, max_tokens=2048, model=MODEL_FAST)
            data = extract_json(raw)
        except Exception as first_exc:
            # Most failures here are token-cap truncation (probabilistic response length),
            # not a content problem, so a second sample is very likely to land under the cap.
            try:
                raw = generate(prompt, max_tokens=2048, model=MODEL_FAST)
                data = extract_json(raw)
            except Exception as second_exc:  # noqa: BLE001 — a single lead's research failure shouldn't kill the run
                exc_text = f"{first_exc}; {second_exc}"
                if "rate_limit_exceeded" in exc_text or " 429" in exc_text:
                    # A Groq TPD/rate-limit hit is this run's fault, not this lead's — the
                    # query filters on research_notes IS NULL, so writing a permanent note
                    # here would exclude the lead from every future run's retry forever, even
                    # once the token budget resets. Leave it untouched so it's picked up again.
                    print(f"[enricher] rate-limited for {lead['domain']}, leaving pending for retry: {exc_text[:200]}")
                    continue
                print(f"[enricher] failed for {lead['domain']} after retry: {second_exc}")
                db.table("outreach_leads").update(
                    {
                        "research_notes": f"[ENRICHMENT_FAILED] generate/parse failed twice: {exc_text}"[:500],
                        "lead_score": lead_score.score,
                        "lead_score_reason": lead_score.reason,
                    }
                ).eq("id", lead["id"]).execute()
                continue

        notes = data.get("research_notes", "").strip()
        if data.get("domestically_made"):
            notes += " (Flag: brand markets itself as domestically/in-house made — weaker outsourcing prospect.)"

        # The LLM is asked for a bare email but sometimes wraps it in a sentence
        # (e.g. "Contact X at y@z.com") despite the prompt — extract the actual
        # address rather than trusting the raw string, so a malformed value never
        # reaches Gmail's API as a To: header.
        raw_email = data.get("contact_email") or ""
        email_match = EMAIL_RE.search(raw_email)
        email = email_match.group(0) if email_match else None
        # Every discovered email goes through email_acceptable(): helpdesk addresses are
        # hard-rejected, email_matches_business still applies, and generic role inboxes
        # (info@/sales@/...) are only kept when the site shows no named person. Lead size
        # is already gated by lead_hunter's verify step, so company_small=True here.
        crawled = None
        if email:
            ok, why = email_acceptable(lead["brand_name"], lead["domain"], email)
            if ok and is_generic_role_email(email):
                # Named contacts first: a generic inbox is only kept if the crawl can't
                # surface a named mailbox on the lead's own domain.
                crawled = crawl_domain_secondary_pages(website_url) or ""
                if crawled and not is_generic_role_email(crawled):
                    ok2, _ = email_acceptable(lead["brand_name"], lead["domain"], crawled)
                    if ok2:
                        email, ok = crawled, True
                        print(f"[enricher] {lead['domain']}: upgraded generic inbox to named contact")
            if ok:
                db.table("outreach_leads").update(
                    {
                        "research_notes": notes,
                        "contact_email": email,
                        "contact_method": "email",
                        "lead_score": lead_score.score,
                        "lead_score_reason": lead_score.reason,
                    }
                ).eq("id", lead["id"]).execute()
                print(f"[enricher] {lead['domain']}: email_found=True (primary)")
                continue
            print(f"[enricher] rejected {email} for {lead['domain']}: {why}")
            if why == "helpdesk_address":
                notes = f"{notes} [rejected helpdesk address at enrichment]".strip()

        if crawled is None:
            crawled = crawl_domain_secondary_pages(website_url)
        if secondary_email := crawled:
            ok, why = email_acceptable(lead["brand_name"], lead["domain"], secondary_email)
            if ok:
                db.table("outreach_leads").update(
                    {
                        "research_notes": notes,
                        "contact_email": secondary_email,
                        "contact_method": "email",
                        "lead_score": lead_score.score,
                        "lead_score_reason": lead_score.reason,
                    }
                ).eq("id", lead["id"]).execute()
                print(f"[enricher] {lead['domain']}: email_found=True (secondary_page_crawl)")
                continue
            print(f"[enricher] rejected {secondary_email} for {lead['domain']}: {why}")

        fallback = discover_contact(
            lead["brand_name"], lead["region"], apollo_max_calls_per_run=APOLLO_MAX_CALLS_PER_RUN
        )
        apollo_outcome = get_last_apollo_outcome()
        if apollo_outcome not in ("not_attempted", "not_configured"):
            notes = f"{notes} [Apollo: {apollo_outcome}]".strip()
            print(f"[enricher] {lead['domain']}: apollo_outcome={apollo_outcome}")

        fallback_email = fallback["email"]
        if fallback_email:
            ok, why = email_acceptable(lead["brand_name"], lead["domain"], fallback_email)
            if not ok:
                print(f"[enricher] rejected {fallback_email} for {lead['domain']} (fallback:{fallback['method']}): {why}")
                fallback_email = None

        if fallback_email:
            db.table("outreach_leads").update(
                {
                    "research_notes": notes,
                    "contact_email": fallback_email,
                    "contact_method": "email",
                    "lead_score": lead_score.score,
                    "lead_score_reason": lead_score.reason,
                }
            ).eq("id", lead["id"]).execute()
            print(f"[enricher] {lead['domain']}: email_found=True (fallback:{fallback['method']})")
        elif fallback["facebook_url"]:
            db.table("outreach_leads").update(
                {
                    "research_notes": f"{notes} Facebook page found: {fallback['facebook_url']}".strip(),
                    "facebook_url": fallback["facebook_url"],
                    "contact_method": "facebook_message_manual_needed",
                    "status": "skipped_no_email",
                    "lead_score": lead_score.score,
                    "lead_score_reason": lead_score.reason,
                }
            ).eq("id", lead["id"]).execute()
            print(f"[enricher] {lead['domain']}: email_found=False, facebook_url found instead")
        else:
            db.table("outreach_leads").update(
                {
                    "research_notes": notes,
                    "contact_method": "contact_form_manual_needed",
                    "status": "skipped_no_email",
                    "lead_score": lead_score.score,
                    "lead_score_reason": lead_score.reason,
                }
            ).eq("id", lead["id"]).execute()
            print(f"[enricher] {lead['domain']}: email_found=False, waterfall found nothing")

    print(f"[enricher] run complete: groq_calls={get_call_count()}, groq_tokens_used={get_tokens_used()}")
    record_tokens("enricher", get_tokens_used(), get_call_count())


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("moto_apparel", "combat_sports"):
        sys.exit("Usage: python scripts/enricher.py <moto_apparel|combat_sports>")
    run(sys.argv[1])
