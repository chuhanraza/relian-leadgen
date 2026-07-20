# Changelog

## 2026-07-20 — Initial build: moto_apparel + combat_sports lead-gen pipeline

- New `leadgen` Postgres schema inside the existing `relian-erp` Supabase project
  (`iuhdisnbmksoxdeumhpf`), isolated from all `public.*` ERP tables. The org's Supabase
  account is capped at 2 free active projects, already used by `relian-erp` and
  `Visit Plane 1 F`, so a standalone `relian-leadgen` project wasn't available — schema
  isolation inside `relian-erp` was chosen as the zero-cost, zero-account-change path.
  Tables: `outreach_leads` (+ `draft_subject`/`draft_body`/`gmail_draft_id`, added beyond
  the original brief so drafted content can persist between the separate Copywriter and
  Sender script runs, and so the Sender doesn't recreate the same Gmail draft on every
  run), `regions_covered`, `daily_run_log`. RLS enabled, zero policies — service-role only.
- Five standalone stage scripts (`lead_hunter.py`, `enricher.py`, `copywriter.py`,
  `sender.py`, `daily_summary.py`) under `scripts/`, sharing `scripts/common/` helpers for
  the Supabase client, Anthropic web-search client, Gmail draft client, and JSON parsing.
- Region rotation lists per vertical in `scripts/common/regions.py`; Mainland China is
  omitted entirely from the combat_sports list per the brief's exclusion.
- Two placeholder spec config files (`config/real_specs_moto_apparel.json`,
  `config/real_specs_combat_sports.json`) — Copywriter refuses to state any technical
  claim until `verified_by_hamad` is manually flipped to `true`.
- `scripts/get_gmail_refresh_token.py` — one-time local OAuth helper Hamad must run
  himself (Google consent can't be completed on his behalf).
- Two staggered GitHub Actions workflows (6am/9am UTC cron + manual `workflow_dispatch`).
- Sender only ever creates Gmail drafts, never sends — hard design constraint, documented
  in README.md "No auto-send".
- No existing glove-buyer pipeline or `relian-leadgen` Supabase project was found during
  Step-0 recon, contrary to the original brief's assumption — this was built from scratch
  and confirmed with Hamad before proceeding (see session for detail).

## 2026-07-20 — Swap research/drafting engine to Gemini (free tier)

- Replaced `scripts/common/claude_client.py` (Anthropic API) with
  `scripts/common/gemini_client.py` (Google Gemini API via `google-genai`), using Gemini's
  free-tier Google Search grounding tool in place of Claude's web_search tool. Reason:
  Hamad wants zero paid services — Anthropic's API has no sustained-use free tier, while
  Gemini's free tier (no card required) comfortably covers this pipeline's actual daily
  volume.
  - `GEMINI_API_KEY` replaces `ANTHROPIC_API_KEY` everywhere: `.env.example`, both GitHub
    Actions workflows, README.
  - `lead_hunter.py`, `enricher.py`, `copywriter.py` now import from `gemini_client`
    instead of `claude_client`.
  - `requirements.txt`: `google-genai` replaces `anthropic`.

## 2026-07-20 — Smoke test found infra gaps + Gemini also required billing; swapped to Groq + DuckDuckGo

- Local smoke test of `lead_hunter.py` surfaced two real Supabase infra gaps not caught
  during initial migration review (PostgREST checks are separate from RLS/migration
  success):
  - `leadgen` schema wasn't in the Data API's exposed-schemas list (defaults to
    `public, graphql_public` only) — fixed via project Settings → Data API → Exposed
    schemas, adding `leadgen`.
  - `service_role` had no Postgres-level GRANT on the new schema — RLS bypass doesn't
    imply schema/table privileges. Migration `social_pipeline_leadgen_service_role_grants`
    adds `GRANT USAGE`/`GRANT ALL` on `leadgen` to `service_role` only, plus
    `ALTER DEFAULT PRIVILEGES` so future tables in this schema inherit the same grant
    automatically.
- Gemini's free tier turned out to require a linked Google Cloud billing account (even at
  $0 actual spend) to raise quota above 0 for new projects — a real card-on-file
  requirement, which conflicts with Hamad's explicit "not a single cent, ever, anywhere"
  constraint. Replaced Gemini entirely with:
  - **Groq** (`scripts/common/groq_client.py`) for all text generation — genuinely free,
    no card required at any tier, ~30 req/min and up to several thousand req/day depending
    on model, comfortably enough for this pipeline's actual volume.
  - **DuckDuckGo search + direct page fetch** (`scripts/common/web_search.py`, via the
    `ddgs` package, no API key at all) replaces AI-driven autonomous browsing for the
    research step. Lead Hunter now runs one DDG search per region and hands the snippets
    to Groq to extract structured candidates; Enricher fetches the brand's actual homepage
    text plus a DDG search for their contact info and hands both to Groq. Tradeoff: this
    is retrieval-then-synthesize rather than a model actively browsing multiple pages per
    lead, so it has a smaller window to find a real email/detail in — it still never
    fabricates one, it just returns `skipped_no_email` more often than a paid tool-use
    model would.
  - `GROQ_API_KEY` replaces `GEMINI_API_KEY` everywhere: `.env.example`, both GitHub
    Actions workflows, README.
  - `requirements.txt`: `groq`, `ddgs`, `requests` replace `google-genai`.

## 2026-07-20 — Fixed real bugs found during full live smoke test (both verticals)

Ran the entire pipeline live end-to-end (not just syntax checks) and fixed what broke:

- **Lead Hunter found 0 leads on the original one-shot search+extract design.** A broad
  query like "boutique motorcycle apparel brand USA" mostly surfaces "15 best brands"
  listicle articles, not the brands' own sites — the model correctly refused to guess a
  domain from listicle content rather than fabricate one, so nothing qualified. Rewrote
  `lead_hunter.py` as a two-step discover → verify pipeline: step 1 extracts candidate
  NAMES only from the broad search (no domain guessing); step 2 runs a second, targeted
  DDG search per name (`"<name>" official website`) plus a homepage fetch, then
  re-verifies brand-vs-manufacturer / sub_type against that specific candidate's own site.
- **Verification was fetching the wrong company's page** when two unrelated companies
  share a similar name (e.g. "WarForged Apparel" vs an unrelated UK "Warforged" fitness
  brand) — it grabbed whichever search result happened to load first, incorrectly
  rejecting the real candidate. Added `_name_match_score()` to rank results by how many
  brand-name tokens appear in the domain before picking which page to fetch and which to
  show the model first. Verified fix: WarForged Apparel now correctly resolves to
  `warforgedapparel.com` instead of being wrongly rejected.
- **Copywriter's signature was unreliable** — the prompt asked the model to "sign the
  email exactly as Hamad, Relian MFG" but Groq/Llama sometimes omitted it entirely (caught
  in a real generated draft). Removed the sign-off instruction from the prompt and append
  a fixed `SIGNATURE` constant in code instead, so it's guaranteed on every draft
  regardless of model behavior.
- Two Supabase infra gaps also needed manual dashboard fixes (see prior entry): schema not
  exposed to the Data API, and `service_role` missing schema-level grants.

Live verification after fixes: real leads found and inserted for both verticals
(`moto_apparel`: Belstaff → belstaff.com; `combat_sports`: 4 leads including
`warforgedapparel.com` with correct `sub_type` classification), Enricher found a real
published email (`customerservice@belstaff.com`) with real research notes (D30 Ghost
armour, waxed cotton), Copywriter drafted a grounded pitch respecting the
`verified_by_hamad: false` gate, Sender created one real Gmail draft in
relianmfg@gmail.com and correctly skipped re-creating it on a second run
(`gmail_draft_id` idempotency check confirmed working).
