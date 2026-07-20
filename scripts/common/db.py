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
