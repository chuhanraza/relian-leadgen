"""Free, no-API-key web research: DuckDuckGo search + direct page fetch. This replaces
the "AI browses the web itself" tool-use approach (Claude/Gemini) — both of those require
a billing account linked even to stay within their free quotas, which is a hard no here.
Instead: search DuckDuckGo for candidates ourselves, hand the model the raw snippets/page
text, and let Groq (free, no card) synthesize a structured answer strictly from that.
"""

import re

import requests
from ddgs import DDGS


def ddg_search(query: str, max_results: int = 8) -> list[dict]:
    try:
        results = DDGS().text(query, max_results=max_results)
    except Exception:  # noqa: BLE001 — search failures shouldn't crash a whole run
        return []
    return [
        {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
        for r in results
    ]


def format_results(results: list[dict]) -> str:
    if not results:
        return "(no search results found)"
    return "\n\n".join(
        f"- {r['title']}\n  URL: {r['url']}\n  Snippet: {r['snippet']}" for r in results
    )


def fetch_page_text(url: str, max_chars: int = 4000) -> str:
    """Best-effort plain-text scrape of a page. Returns '' on any failure — callers must
    treat that as 'no page content available', not an error.
    """
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        return ""
    text = re.sub(
        r"<script.*?</script>|<style.*?</style>", "", resp.text, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]
