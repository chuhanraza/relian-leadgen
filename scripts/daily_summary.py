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
        .select("region")
        .eq("vertical", vertical)
        .gte("last_searched_at", today)
        .execute()
        .data
    )

    row = {
        "vertical": vertical,
        "run_date": today,
        "leads_found": found.count or 0,
        "leads_drafted": drafted.count or 0,
        "leads_skipped_no_email": skipped.count or 0,
        "leads_excluded_wrong_type": excluded.count or 0,
        "regions_covered_this_run": [r["region"] for r in regions],
    }
    db.table("daily_run_log").insert(row).execute()
    print(f"[daily_summary] {row}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("moto_apparel", "combat_sports"):
        sys.exit("Usage: python scripts/daily_summary.py <moto_apparel|combat_sports>")
    run(sys.argv[1])
