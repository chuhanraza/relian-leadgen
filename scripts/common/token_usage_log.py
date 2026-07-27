"""Cross-process Groq token-usage log for a single pipeline run.

lead_hunter / enricher / copywriter / daily_summary each run as a SEPARATE
`python scripts/X.py` process within the same GitHub Actions job (see
.github/workflows/combat_sports.yml and moto_apparel.yml), sharing the same
checked-out workspace but not memory. This file is how a stage's real Groq
usage (scripts/common/groq_client.get_tokens_used()) survives its process
exiting, so daily_summary.py — the last step — can roll it into
leadgen.daily_run_log and make it queryable in Supabase. The file lives in
the job's workspace, which is thrown away after every job run, so it never
carries stale numbers into the next run.
"""

import json
import os

_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", ".groq_usage_run.json")


def record(stage: str, tokens: int, calls: int) -> None:
    data = _read()
    data[stage] = {"tokens": tokens, "calls": calls}
    with open(_LOG_PATH, "w") as f:
        json.dump(data, f)


def read_and_clear() -> dict:
    data = _read()
    if os.path.exists(_LOG_PATH):
        os.remove(_LOG_PATH)
    return data


def _read() -> dict:
    if not os.path.exists(_LOG_PATH):
        return {}
    with open(_LOG_PATH) as f:
        return json.load(f)
