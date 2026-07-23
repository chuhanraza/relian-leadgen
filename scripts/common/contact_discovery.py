"""Fallback contact-discovery waterfall for leads with no email found via the
normal website route. Tries, in order: a deeper crawl of the lead's OWN domain
(sitemap.xml / secondary pages like /contact, /about) -> OpenStreetMap Overpass
API (real free/no-key API, real business contact tags) -> DuckDuckGo
Facebook-page dork (reuses the existing ddg_search, which already has
retry/pacing/failure tracking — do not build a second parallel scraper) ->
Instagram/Linktree resolution -> Apollo.io (last resort, verified emails only).
Every step is best-effort; any failure just falls through to the next stage.
Returns None if nothing is found (caller must NOT invent a pattern email).
"""

from __future__ import annotations

import os
import re
import urllib.parse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from .web_search import ddg_search, fetch_page_text

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
IGNORED_EMAIL_DOMAINS = {"facebook.com", "instagram.com", "google.com", "sentry.io", "wix.com", "wordpress.com"}
IGNORE_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".css", ".js", ".svg", ".webp")
CONTACT_SLUGS = (
    "contact", "about", "us", "privacy", "terms", "impressum", "wholesale", "support", "faq",
)
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
GENERIC_EMAIL_PROVIDERS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}


def _valid_email(candidates: list[str]) -> str | None:
    for e in candidates:
        if not any(d in e.lower() for d in IGNORED_EMAIL_DOMAINS):
            return e
    return None


def _domain_root(d: str) -> str:
    """Strip protocol/path/subdomain noise for comparison, e.g.
    'https://www.united-fightwear.com/contact' -> 'united-fightwear.com'
    """
    d = re.sub(r"^https?://", "", d.lower()).split("/")[0]
    parts = d.split(".")
    return ".".join(parts[-2:]) if len(parts) > 1 else d


def email_matches_business(brand_name: str, lead_domain: str, candidate_email: str) -> bool:
    """Reject a discovered email that doesn't plausibly belong to the SAME
    business as the lead. A name-based search step (Facebook dork, Instagram,
    Apollo) can surface a plausible-sounding but wrong result for common/generic
    business names (e.g. a different real company entirely) — this must run
    before any discovered email is accepted anywhere in the pipeline.

    Passes if either:
    1. The email's domain root matches the lead's own domain root (strongest signal).
    2. The email is on a generic personal provider (gmail etc.) AND the local-part
       shares a meaningful token with the brand name.
    Otherwise rejects.
    """
    email_domain = _domain_root(candidate_email.split("@")[-1])
    lead_root = _domain_root(lead_domain)

    if email_domain == lead_root:
        return True

    if email_domain in GENERIC_EMAIL_PROVIDERS:
        local_part = candidate_email.split("@")[0].lower()
        brand_tokens = re.findall(r"[a-z0-9]+", brand_name.lower())
        return any(len(t) > 2 and t in local_part for t in brand_tokens)

    return False


def _contact_urls_from_sitemap(website_url: str) -> list[str]:
    try:
        resp = requests.get(
            urllib.parse.urljoin(website_url, "/sitemap.xml"), timeout=8, headers=REQUEST_HEADERS
        )
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
        return [u for u in locs if any(slug in u.lower() for slug in CONTACT_SLUGS)]
    except Exception:  # noqa: BLE001 — best-effort fallback stage
        return []


def _contact_urls_from_homepage_links(website_url: str) -> list[str]:
    try:
        home = requests.get(website_url, timeout=8, headers=REQUEST_HEADERS)
        soup = BeautifulSoup(home.text, "html.parser")
        base_netloc = urllib.parse.urlparse(website_url).netloc
        urls = []
        for a in soup.find_all("a", href=True):
            full = urllib.parse.urljoin(website_url, a["href"])
            if urllib.parse.urlparse(full).netloc == base_netloc and any(
                slug in full.lower() for slug in CONTACT_SLUGS
            ):
                urls.append(full)
        return urls
    except Exception:  # noqa: BLE001 — best-effort fallback stage
        return []


def crawl_domain_secondary_pages(website_url: str, max_pages: int = 5) -> str | None:
    """Checks /sitemap.xml for contact-relevant URLs (parsed with the stdlib XML
    parser, not BeautifulSoup's lxml-backed 'xml' mode, so this stays dependency-
    light); falls back to parsing homepage internal links if no sitemap. Crawls
    up to max_pages, stops at the first genuine email found. mailto: links are
    checked before body-text regex. Filters obvious image/asset false-positives.
    """
    if not website_url.startswith("http"):
        website_url = f"https://{website_url}"

    urls_to_check = _contact_urls_from_sitemap(website_url) or _contact_urls_from_homepage_links(
        website_url
    )
    if not urls_to_check:
        return None

    for url in urls_to_check[:max_pages]:
        try:
            resp = requests.get(url, timeout=8, headers=REQUEST_HEADERS)
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception:  # noqa: BLE001 — best-effort fallback stage
            continue

        for a in soup.find_all("a", href=True):
            if a["href"].lower().startswith("mailto:"):
                email = a["href"].split(":", 1)[1].split("?")[0].strip().lower()
                if EMAIL_RE.match(email):
                    return email

        for match in EMAIL_RE.findall(soup.get_text(" ")):
            e = match.lower().rstrip(".")
            if not any(e.endswith(s) for s in IGNORE_EMAIL_SUFFIXES):
                return e
    return None


def try_overpass(brand_name: str, region: str) -> str | None:
    """OpenStreetMap Overpass API — free, no key required. Use a proper
    descriptive User-Agent (required by OSM usage policy, and community
    reports show generic/missing User-Agents get 406'd). Search by name
    match within the region is approximate since we don't have precise
    coordinates — treat this as best-effort, not primary.
    """
    query = f"""
    [out:json][timeout:15];
    area["name"~"{region.split('/')[0]}",i]->.searchArea;
    (
      node["name"~"{brand_name}",i](area.searchArea);
      way["name"~"{brand_name}",i](area.searchArea);
    );
    out tags 5;
    """
    try:
        resp = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            headers={"User-Agent": "RelianMFG-LeadGen/1.0 (contact: hm@relianmfg.com)"},
            timeout=15,
        )
        resp.raise_for_status()
        for el in resp.json().get("elements", []):
            tags = el.get("tags", {})
            if tags.get("contact:email"):
                return tags["contact:email"]
    except Exception:  # noqa: BLE001 — best-effort fallback stage
        return None
    return None


def try_facebook_dork(brand_name: str, region: str) -> tuple[str | None, str | None]:
    """Returns (email, facebook_url). Searches DDG for the business's Facebook
    Page and regexes any published email out of the indexed snippet text —
    does NOT fetch facebook.com directly (returns a login wall, not content).
    """
    results = ddg_search(f'"{brand_name}" {region} email OR contact site:facebook.com', max_results=5)
    fb_url = next((r["url"] for r in results if "facebook.com" in r["url"]), None)
    snippet_text = " ".join(r["snippet"] for r in results)
    email = _valid_email(EMAIL_RE.findall(snippet_text))
    return email, fb_url


def try_instagram_linktree(brand_name: str, region: str) -> str | None:
    """Searches DDG for an Instagram bio, and if a Linktree/Beacons link is
    exposed in the snippet, fetches that page directly (linktr.ee is a plain
    static page, unlike Instagram/Facebook — safe to fetch normally).
    """
    results = ddg_search(f'"{brand_name}" {region} site:instagram.com', max_results=3)
    snippet_text = " ".join(r["snippet"] for r in results)
    linktree_match = re.search(r"(linktr\.ee/\w+|beacons\.ai/\w+)", snippet_text)
    if not linktree_match:
        return None
    page_text = fetch_page_text(f"https://{linktree_match.group(1)}")
    return _valid_email(EMAIL_RE.findall(page_text))


APOLLO_BASE_URL = "https://api.apollo.io/api/v1"
# Hard rule (not a preference): only this exact Apollo email_status counts as a real
# hit. "likely to engage", "unverified", "unavailable" etc. are all guessed/probabilistic
# and must be rejected the same as no match — accepting them here defeats the entire
# point of this pipeline's no-guessing contact policy.
APOLLO_VERIFIED_STATUS = "verified"

# Module-level, same pattern as web_search.py's ddg failure counter — lets enricher.py's
# run() enforce a hard per-run spend cap (APOLLO_MAX_CALLS_PER_RUN) across many leads
# without threading a counter through every function signature.
_apollo_call_count = 0
_last_apollo_outcome = "not_attempted"


def get_apollo_call_count() -> int:
    return _apollo_call_count


def reset_apollo_call_count() -> None:
    global _apollo_call_count
    _apollo_call_count = 0


def get_last_apollo_outcome() -> str:
    """One of: not_attempted, not_configured, cap_reached, no_org_found,
    no_people_found, rejected_unverified:<status>, verified_email_found.
    """
    return _last_apollo_outcome


def _apollo_request(path: str, payload: dict, api_key: str) -> dict | None:
    try:
        resp = requests.post(
            f"{APOLLO_BASE_URL}/{path}",
            json=payload,
            headers={"x-api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — best-effort last-resort stage
        print(f"[contact_discovery] apollo request failed ({path}): {exc}")
        return None


def try_apollo(brand_name: str, region: str, max_calls_per_run: int = 5) -> str | None:
    """Last resort: Apollo.io organization search -> people search -> people match,
    called against Apollo's real REST API via APOLLO_API_KEY. This is a DIFFERENT
    credential from any MCP/Apollo connector — this pipeline runs unattended in
    GitHub Actions, which cannot reach an interactive session's MCP connection, so
    it needs its own API key (a repo secret, same pattern as GROQ_API_KEY).

    Guarded by max_calls_per_run (see APOLLO_MAX_CALLS_PER_RUN in enricher.py) because
    every lead that reaches this stage already failed every free method, and each
    attempt here can spend real Apollo credits — capping keeps a single unattended
    cron run from spending unbounded credits across a whole day's lead volume.

    Only returns an email when Apollo's own verification status is exactly
    "verified" — never "guessed"/"likely to engage"/"unverified"/"unavailable".
    """
    global _apollo_call_count, _last_apollo_outcome

    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        _last_apollo_outcome = "not_configured"
        return None

    if _apollo_call_count >= max_calls_per_run:
        _last_apollo_outcome = "cap_reached"
        return None
    _apollo_call_count += 1

    org_data = _apollo_request(
        "mixed_companies/search",
        {"q_organization_name": brand_name, "organization_locations": [region], "per_page": 1},
        api_key,
    )
    orgs = (org_data or {}).get("organizations") or []
    if not orgs:
        _last_apollo_outcome = "no_org_found"
        return None
    domain = orgs[0].get("primary_domain") or orgs[0].get("website_url") or orgs[0].get("domain")
    if not domain:
        _last_apollo_outcome = "no_org_found"
        return None

    people_data = _apollo_request(
        "mixed_people/search",
        {
            "q_organization_domains_list": [domain],
            "person_seniorities": ["owner", "founder", "c_suite", "director"],
            "per_page": 1,
        },
        api_key,
    )
    people = (people_data or {}).get("people") or []
    if not people:
        _last_apollo_outcome = "no_people_found"
        return None
    person = people[0]

    match_data = _apollo_request(
        "people/match",
        {
            "first_name": person.get("first_name"),
            "last_name": person.get("last_name"),
            "organization_name": brand_name,
            "domain": domain,
        },
        api_key,
    )
    matched = (match_data or {}).get("person") or {}
    email = matched.get("email")
    status = (matched.get("email_status") or "").strip().lower()

    if email and status == APOLLO_VERIFIED_STATUS:
        _last_apollo_outcome = "verified_email_found"
        return email

    _last_apollo_outcome = f"rejected_unverified:{status or 'no_status'}"
    return None


def discover_contact(brand_name: str, region: str, apollo_max_calls_per_run: int = 5) -> dict:
    """Runs the full waterfall. Returns:
    {"email": str|None, "facebook_url": str|None, "method": str}
    method is one of: overpass, facebook_dork, instagram_linktree, apollo, none

    Note: crawl_domain_secondary_pages() (a deeper crawl of the LEAD'S OWN site) is
    NOT part of this waterfall — enricher.py calls it separately, before this
    function, since it's re-checking the same known domain rather than a new source.
    """
    if email := try_overpass(brand_name, region):
        return {"email": email, "facebook_url": None, "method": "overpass"}

    email, fb_url = try_facebook_dork(brand_name, region)
    if email:
        return {"email": email, "facebook_url": fb_url, "method": "facebook_dork"}

    if email := try_instagram_linktree(brand_name, region):
        return {"email": email, "facebook_url": fb_url, "method": "instagram_linktree"}

    if email := try_apollo(brand_name, region, max_calls_per_run=apollo_max_calls_per_run):
        return {"email": email, "facebook_url": fb_url, "method": "apollo"}

    return {"email": None, "facebook_url": fb_url, "method": "none"}
