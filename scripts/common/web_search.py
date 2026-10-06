"""Free, no-API-key web research: DuckDuckGo search + direct page fetch. This replaces
the "AI browses the web itself" tool-use approach (Claude/Gemini) — both of those require
a billing account linked even to stay within their free quotas, which is a hard no here.
Instead: search DuckDuckGo for candidates ourselves, hand the model the raw snippets/page
text, and let Groq (free, no card) synthesize a structured answer strictly from that.
"""

import re
import time

import requests
from ddgs import DDGS

DDG_RETRY_WAIT_SECONDS = 5
DDG_PACING_SECONDS = 1.5

# Module-level counter, not per-call return value, so callers (lead_hunter) can read a
# running total across many ddg_search() calls without threading a counter through every
# function signature. Reset at the start of each run() invocation.
_ddg_failure_count = 0


def get_ddg_failure_count() -> int:
    return _ddg_failure_count


def reset_ddg_failure_count() -> None:
    global _ddg_failure_count
    _ddg_failure_count = 0


# ddgs picks a RANDOM browser-impersonation profile per DDGS() instance (secrets.choice in
# ddgs/http_client.py). Some profiles raise `ValueError: Unsupported protocol version 0x304`
# instantly (~25% of calls in a 2026-10-06 trace, no pattern by query or region). A fresh
# DDGS() draws a new profile, so an immediate retry almost always succeeds — no sleep needed.
DDG_MAX_ATTEMPTS = 4
_TLS_PROFILE_ERROR = "Unsupported protocol version"

_ddg_retry_saves = 0  # searches that failed at least once but succeeded on a retry


def get_ddg_retry_saves() -> int:
    return _ddg_retry_saves


def _ddg_search_with_retries(query: str, max_results: int):
    """Returns a list of results (possibly empty = genuinely no results), or None if every
    attempt failed with a real error.
    """
    global _ddg_retry_saves
    for attempt in range(DDG_MAX_ATTEMPTS):
        try:
            results = DDGS().text(query, max_results=max_results)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "No results found" in msg:
                return []  # a real empty answer, not a failure
            if _TLS_PROFILE_ERROR in msg:
                continue  # bad impersonation profile: retry immediately with a fresh one
            if attempt < DDG_MAX_ATTEMPTS - 1:
                time.sleep(DDG_RETRY_WAIT_SECONDS * (attempt + 1))  # rate-limit/timeout backoff
            continue
        if attempt:
            _ddg_retry_saves += 1
        return [
            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in results
        ]
    return None


def ddg_search(query: str, max_results: int = 8) -> list[dict]:
    """Up to DDG_MAX_ATTEMPTS attempts (see _ddg_search_with_retries), then a fixed 1.5s
    pacing delay before returning either way — keeps us from bursting requests at
    DuckDuckGo as call volume scales up, and surfaces persistent failures via the
    module-level counter instead of silently returning an empty list indistinguishable
    from "no results found".
    """
    global _ddg_failure_count

    result = _ddg_search_with_retries(query, max_results)
    if result is None:
        _ddg_failure_count += 1
        result = []

    time.sleep(DDG_PACING_SECONDS)
    return result


def format_results(results: list[dict]) -> str:
    if not results:
        return "(no search results found)"
    return "\n\n".join(
        f"- {r['title']}\n  URL: {r['url']}\n  Snippet: {r['snippet']}" for r in results
    )


def html_to_text(html: str, max_chars: int = 4000) -> str:
    """Strip scripts/styles/tags down to plain text, same rule fetch_page_text has always
    used — pulled out standalone so a caller that already has raw HTML in memory (e.g.
    Enricher's lead-scoring signal, which needs the tags fetch_page_text throws away) can
    derive plain text from it too, without a second network request.
    """
    text = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def fetch_page_html(url: str, max_chars: int = 200_000) -> str:
    """Best-effort raw HTML fetch. Returns '' on any failure — callers must treat that as
    'no page content available', not an error.
    """
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        return ""
    return resp.text[:max_chars]


def fetch_page_text(url: str, max_chars: int = 4000) -> str:
    """Best-effort plain-text scrape of a page. Returns '' on any failure — callers must
    treat that as 'no page content available', not an error.
    """
    html = fetch_page_html(url, max_chars=200_000)
    if not html:
        return ""
    return html_to_text(html, max_chars)
