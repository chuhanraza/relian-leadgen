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
