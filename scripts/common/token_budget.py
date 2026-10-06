"""Per-run Groq token budgets, derived from the free-tier daily limits instead of a flat
guess. Each key's quota is per ORGANIZATION per model (gpt-oss-20b and gpt-oss-120b each
get 200K tokens/day), and every vertical runs 4x/day, so:

    per-run budget (per model) = DAILY_LIMIT * 90% / 4 runs = 45,000 tokens

Stages are spread over BOTH models so neither is exhausted: discovery + copywriter on the
(otherwise idle) 120B, verify + enricher on the 20B. Within a model, the per-run budget is
split between the stages that use it. Override the daily limit with GROQ_DAILY_TOKEN_LIMIT
if the plan changes (e.g. billing turned on).
"""

from __future__ import annotations

import os

DAILY_TOKEN_LIMIT = int(os.environ.get("GROQ_DAILY_TOKEN_LIMIT") or "200000")  # per model, free tier
DAILY_USE_FRACTION = 0.90
RUNS_PER_DAY = 4

PER_RUN_BUDGET_PER_MODEL = int(DAILY_TOKEN_LIMIT * DAILY_USE_FRACTION / RUNS_PER_DAY)  # 45,000

# Share of the per-run, per-model budget given to each stage (shares on one model sum <= 1).
STAGE_SHARE = {
    # gpt-oss-120b: discovery (long listicle prompts) + copywriter bodies
    "discover": ("120b", 0.65),
    "copywriter": ("120b", 0.35),
    # gpt-oss-20b: verify + enricher (+ small copywriter image-pick calls)
    "verify": ("20b", 0.50),
    "enricher": ("20b", 0.42),
    "copywriter_fast": ("20b", 0.08),
}


def stage_budget(stage: str) -> int:
    return int(PER_RUN_BUDGET_PER_MODEL * STAGE_SHARE[stage][1])
