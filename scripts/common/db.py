"""Supabase client for the leadgen schema inside the relian-erp project.

Uses the service-role key (server-side only, never expose it) and points the client at
the `leadgen` Postgres schema so all queries default there instead of `public` — this
pipeline's tables are isolated from the ERP's own public.* tables.
"""

import os

from supabase import Client, ClientOptions, create_client


def get_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key, options=ClientOptions(schema="leadgen"))


PAGE_SIZE = 1000  # Supabase/PostgREST silently caps a single response at 1,000 rows.


def fetch_all(build_query, order_by: str = "id", page_size: int = PAGE_SIZE) -> list[dict]:
    """Returns EVERY row of a select, paging with .range() in page_size chunks.

    build_query is a zero-arg callable returning a fresh, un-executed select query (filters
    already applied, no limit/range). A fresh query per page keeps the builder state clean,
    and the explicit order makes pages stable so rows are neither skipped nor repeated.
    Without this, a bare .select().execute() returns at most 1,000 rows and any dedupe built
    on it silently forgets everything past that point.
    """
    rows: list[dict] = []
    start = 0
    while True:
        page = build_query().order(order_by).range(start, start + page_size - 1).execute().data or []
        rows.extend(page)
        if len(page) < page_size:
            return rows
        start += page_size


def insert_lead_once(db, row: dict) -> bool:
    """Idempotent insert into outreach_leads keyed on (vertical, domain). Returns True if a
    row was inserted, False if that (vertical, domain) already existed. Does a keyed lookup
    first (so it never depends on a page of existing rows) and treats a unique-violation
    race as 'already exists'."""
    existing = (
        db.table("outreach_leads")
        .select("id")
        .eq("vertical", row["vertical"])
        .eq("domain", row["domain"])
        .limit(1)
        .execute()
        .data
    )
    if existing:
        return False
    try:
        db.table("outreach_leads").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        if "23505" in str(exc) or "duplicate key" in str(exc).lower():
            return False
        raise
    return True
