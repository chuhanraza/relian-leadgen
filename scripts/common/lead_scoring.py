"""Lightweight heuristic lead-scoring signal — pure Python/regex, no network or Groq calls.

Scores the raw HTML Enricher already fetches for a lead's homepage (see enricher.py) on
three signals that correlate with "outsourcing-curious apparel/gear brand" without needing
an LLM judgment call on every lead:

1. Outsourcing vs. domestic-manufacturing language — the strongest signal. A brand that
   already talks about overseas manufacturing partners is a warmer prospect than one that
   markets in-house/domestic production as a selling point; the latter is treated as a
   near-disqualification rather than just a weaker lead.
2. E-commerce platform + marketing-stack presence — Shopify/WooCommerce plus Klaviyo/
   Yotpo/Gorgias script tags are a rough proxy for real transaction volume (paid retention
   tooling implies enough revenue to justify it).
3. Physical distribution language ("stockists", "dealers", "wholesale") — signals an
   existing wholesale/retail operation, the kind of brand more likely to need a
   manufacturing partner at scale.

This is Phase 2 only (see CHANGELOG) — Phase 1 (upgrading off the Groq free tier) is
explicitly out of scope. The resulting score is recorded on outreach_leads.lead_score
alongside Enricher's existing work; it does not gate or filter anything yet. Hamad wants
to see the real score distribution across a few days of live leads before picking a cutoff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .web_search import html_to_text

_OUTSOURCING_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"made in (pakistan|vietnam|china|bangladesh|india|cambodia|indonesia|turkey)",
        r"our manufacturing partners?",
        r"manufactur(ed|ing) (overseas|abroad)",
        r"imported materials?",
        r"sourced (overseas|globally|internationally)",
        r"contract manufactur\w*",
        r"private label\w*",
    ]
]

_DOMESTIC_DISQUALIFY_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"handmade in our (uk|usa|us|united kingdom|united states|england|america)\w* workshop",
        r"cut\s*(and|&)\s*sew in[- ]house",
        r"100%\s*domestically made",
        r"100%\s*made in (the )?(usa|uk)",
    ]
]

_SHOPIFY_MARKERS = [
    re.compile(p, re.IGNORECASE)
    for p in [r"cdn\.shopify\.com", r"myshopify\.com", r"shopify-checkout-api-token", r"Shopify\.theme"]
]
_WOOCOMMERCE_MARKERS = [
    re.compile(p, re.IGNORECASE) for p in [r"woocommerce", r"wp-content/plugins/woocommerce", r"wc-ajax"]
]
_MARKETING_STACK_MARKERS = {
    "klaviyo": re.compile(r"klaviyo", re.IGNORECASE),
    "yotpo": re.compile(r"yotpo", re.IGNORECASE),
    "gorgias": re.compile(r"gorgias", re.IGNORECASE),
}

_DISTRIBUTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [r"store locator", r"stockists?", r"\bdealers?\b", r"wholesale inquir(y|ies)", r"find a (store|dealer)"]
]

# Neutral starting point: a lead with no detected signal either way lands at 50, and
# individual signals nudge it up or down from there rather than building the score up
# from zero (which would understate leads whose site just doesn't say much either way).
_BASE_SCORE = 50


@dataclass
class LeadScore:
    score: int
    reason: str
    disqualified: bool


def score_lead(html: str) -> LeadScore:
    if not html:
        return LeadScore(score=_BASE_SCORE, reason="no HTML available, neutral default", disqualified=False)

    text = html_to_text(html, max_chars=20_000)
    reasons: list[str] = []

    domestic_hits = [p.pattern for p in _DOMESTIC_DISQUALIFY_PATTERNS if p.search(text)]
    outsourcing_hits = [p.pattern for p in _OUTSOURCING_PATTERNS if p.search(text)]

    if domestic_hits and not outsourcing_hits:
        reasons.append(f"disqualifying domestic-manufacturing language ({len(domestic_hits)} match(es))")
        return LeadScore(score=5, reason="; ".join(reasons), disqualified=True)

    score = _BASE_SCORE

    if outsourcing_hits:
        score += min(30, 15 * len(outsourcing_hits))
        reasons.append(f"outsourcing language found ({len(outsourcing_hits)} match(es))")
    if domestic_hits:
        # Present alongside outsourcing language -- ambiguous, not disqualifying, but
        # still a headwind worth reflecting in the score.
        score -= 10
        reasons.append(f"some domestic-manufacturing language also present ({len(domestic_hits)} match(es))")

    shopify = any(p.search(html) for p in _SHOPIFY_MARKERS)
    woocommerce = any(p.search(html) for p in _WOOCOMMERCE_MARKERS)
    if shopify or woocommerce:
        score += 15
        reasons.append(f"e-commerce platform detected ({'Shopify' if shopify else 'WooCommerce'})")

    marketing_hits = [name for name, pat in _MARKETING_STACK_MARKERS.items() if pat.search(html)]
    if marketing_hits:
        score += 10 * min(2, len(marketing_hits))
        reasons.append(f"marketing stack present ({', '.join(marketing_hits)}) — proxy for transaction volume")

    distribution_hits = [p.pattern for p in _DISTRIBUTION_PATTERNS if p.search(text)]
    if distribution_hits:
        score += min(20, 10 * len(distribution_hits))
        reasons.append(f"physical distribution language found ({len(distribution_hits)} match(es))")

    score = max(0, min(100, score))
    if not reasons:
        reasons.append("no strong signals either way")

    return LeadScore(score=score, reason="; ".join(reasons), disqualified=False)
