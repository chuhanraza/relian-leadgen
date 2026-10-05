"""Mark EICMA contacts as suppressed so the campaign never drafts for them again.

Manual use when someone replies (auto reply detection needs gmail.readonly, which was
deliberately dropped). Only touches leadgen.eicma_invitations.

Usage:
  python scripts/eicma_suppress.py person@example.com other@example.com
"""

import sys

from dotenv import load_dotenv

from common.db import get_client

load_dotenv()


def main(emails: list[str]) -> int:
    if not emails:
        print(__doc__)
        return 2
    db = get_client()
    missing = 0
    for raw in emails:
        email = raw.strip().lower()
        rows = db.table("eicma_invitations").select("id,email,suppressed").ilike("email", email).execute().data
        if not rows:
            print(f"[eicma_suppress] NOT FOUND: {email}")
            missing += 1
            continue
        for row in rows:
            if row["suppressed"]:
                print(f"[eicma_suppress] already suppressed: {row['email']}")
                continue
            db.table("eicma_invitations").update({"suppressed": True}).eq("id", row["id"]).execute()
            print(f"[eicma_suppress] suppressed: {row['email']}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
