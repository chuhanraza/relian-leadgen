"""Stage 5: Daily Summary. Run last, after all other stages. Writes today's counts to
leadgen.daily_run_log by querying rows touched today, plus which regions were covered.

Usage:
  python scripts/daily_summary.py moto_apparel
  python scripts/daily_summary.py combat_sports
"""

import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from common.db import get_client
from common.token_usage_log import read_and_clear as read_and_clear_tokens

load_dotenv()


def run(vertical: str) -> None:
    db = get_client()
    today = datetime.now(timezone.utc).date().isoformat()

    found = (
        db.table("outreach_leads")
        .select("id", count="exact")
        .eq("vertical", vertical)
        .gte("created_at", today)
        .execute()
    )
    drafted = (
        db.table("outreach_leads")
        .select("id", count="exact")
        .eq("vertical", vertical)
        .eq("status", "drafted")
        .gte("drafted_at", today)
        .execute()
    )
    skipped = (
        db.table("outreach_leads")
        .select("id", count="exact")
        .eq("vertical", vertical)
        .eq("status", "skipped_no_email")
        .gte("created_at", today)
        .execute()
    )
    excluded = (
        db.table("outreach_leads")
        .select("id", count="exact")
        .eq("vertical", vertical)
        .eq("status", "excluded_wrong_type")
        .gte("created_at", today)
        .execute()
    )
    regions = (
        db.table("regions_covered")
        .select("region, ddg_failures")
        .eq("vertical", vertical)
        .gte("last_searched_at", today)
        .execute()
        .data
    )
    ddg_failures_today = sum(r.get("ddg_failures") or 0 for r in regions)

    # lead_hunter/enricher/copywriter each run as a separate process in this same GH
    # Actions job and record their own real Groq usage to a shared workspace file
    # (common/token_usage_log.py) since they can't share memory. This is the last step,
    # so it rolls that up into one queryable number plus a per-stage breakdown note.
    token_usage = read_and_clear_tokens()
    groq_tokens_used_today = sum(stage.get("tokens", 0) for stage in token_usage.values())
    token_breakdown = ", ".join(
        f"{stage}={data.get('tokens', 0)}tok/{data.get('calls', 0)}calls"
        for stage, data in token_usage.items()
    )

    row = {
        "vertical": vertical,
        "run_date": today,
        "leads_found": found.count or 0,
        "leads_drafted": drafted.count or 0,
        "leads_skipped_no_email": skipped.count or 0,
        "leads_excluded_wrong_type": excluded.count or 0,
        "regions_covered_this_run": [r["region"] for r in regions],
        "ddg_failures": ddg_failures_today,
        "groq_tokens_used": groq_tokens_used_today,
        "notes": f"groq usage: {token_breakdown}" if token_breakdown else None,
    }
    db.table("daily_run_log").insert(row).execute()
    print(f"[daily_summary] {row}")

    if ddg_failures_today > 0:
        print(
            f"[daily_summary] WARNING: {ddg_failures_today} DuckDuckGo search(es) failed "
            f"today after retry — possible throttling. Check regions_covered.ddg_failures "
            f"for {vertical} if leads_found looks low."
        )


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("moto_apparel", "combat_sports"):
        sys.exit("Usage: python scripts/daily_summary.py <moto_apparel|combat_sports>")
    run(sys.argv[1])
