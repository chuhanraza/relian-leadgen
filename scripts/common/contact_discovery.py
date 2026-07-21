"""Fallback contact-discovery waterfall for leads with no email found via the
normal website route. Tries, in order: OpenStreetMap Overpass API (real
free/no-key API, real business contact tags) -> DuckDuckGo Facebook-page
dork (reuses the existing ddg_search, which already has retry/pacing/failure
tracking — do not build a second parallel scraper) -> Instagram/Linktree
resolution. Every step is best-effort; any failure just falls through to the
next stage. Returns None if nothing is found (caller must NOT invent a
pattern email).
"""

from __future__ import annotations

import re

import requests

from .web_search import ddg_search, fetch_page_text

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
IGNORED_EMAIL_DOMAINS = {"facebook.com", "instagram.com", "google.com", "sentry.io", "wix.com", "wordpress.com"}


def _valid_email(candidates: list[str]) -> str | None:
    for e in candidates:
        if not any(d in e.lower() for d in IGNORED_EMAIL_DOMAINS):
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


def discover_contact(brand_name: str, region: str) -> dict:
    """Runs the full waterfall. Returns:
    {"email": str|None, "facebook_url": str|None, "method": str}
    method is one of: overpass, facebook_dork, instagram_linktree, none
    """
    if email := try_overpass(brand_name, region):
        return {"email": email, "facebook_url": None, "method": "overpass"}

    email, fb_url = try_facebook_dork(brand_name, region)
    if email:
        return {"email": email, "facebook_url": fb_url, "method": "facebook_dork"}

    if email := try_instagram_linktree(brand_name, region):
        return {"email": email, "facebook_url": fb_url, "method": "instagram_linktree"}

    return {"email": None, "facebook_url": fb_url, "method": "none"}
