# Changelog

## 2026-07-21 — Fix Copywriter crash-cascade; found Groq daily token limit is the real ceiling

First real scheduled run under the new 4x/16x scaling (moto_apparel, `run #2`) failed at
the Copywriter step. Root cause, reproduced directly against the actual leads that
triggered it: **Groq's free-tier daily token limit for `llama-3.3-70b-versatile` (100,000
tokens/day) was nearly exhausted by a single moto_apparel run** —
`Used 99586`/`100000` per the actual error. Lead Hunter's own heavy per-run usage (up to
4 regions x 15 candidates x 1 verify call each, each with sizeable page-text context) plus
Enricher plus Copywriter now burns close to the whole day's shared token budget in one
vertical's one run — and it's a shared budget: both verticals use the same `GROQ_API_KEY`.

Real bug this exposed and fixed: `copywriter.py`'s main draft-writing `generate()` call had
no error handling (unlike `enricher.py` and `select_images()`, which already do) — one
Groq failure crashed the entire script immediately, skipping every remaining lead in the
loop AND the Sender/Daily-Summary steps after it (they showed `skipped` in the run, not
`failure`, but nothing useful happened for the rest of that day). Wrapped the per-lead
drafting logic in the same try/except pattern already used elsewhere: a failed lead is
logged and left in `status='researched'` to retry next run, instead of crashing the whole
job. Verified live: rerunning against the actual still-pending lead now logs the failure
and exits cleanly (0) instead of crashing.

**Not fixed, flagged for Hamad to decide**: the underlying token-budget ceiling itself.
At the current 4x16x scale, one vertical's one run can consume nearly the entire shared
daily Groq token budget, which means combat_sports' scheduled runs today are likely to hit
the same wall, and the ~30/day/vertical target is probably not achievable within Groq's
free 100k-tokens/day limit as currently scaled. See session discussion for options (scale
back down, reduce per-call context/prompt size, spread verticals across separate Groq
accounts, accept partial daily throughput) — no unilateral change made to the scaling
parameters shipped in the prior entry, since that's a real tradeoff decision, not a bug.

## 2026-07-21 — Scale Lead Hunter toward ~30 drafted leads/day/vertical

Scaled Lead Hunter to 4 regions/run x 15 candidates/region x 4 runs/day per vertical (up
from 1x1x1) — target ~30 drafted/day/vertical. Actual yield depends on real candidate
availability and will need a few days of real data to confirm it's hitting target.

- `scripts/common/web_search.py` — `ddg_search()` now retries once after a 5s wait on
  failure, sleeps 1.5s after every call as fixed pacing, and tracks failures via a
  module-level counter (`get_ddg_failure_count()`/`reset_ddg_failure_count()`) instead of
  silently returning `[]` indistinguishable from "no results found". Signature and return
  type unchanged. Verified both the real success path and a simulated failure path
  (mocked to force both attempts to raise) — timing and failure count both correct.
- `scripts/lead_hunter.py` — `discover_names()` cap raised from 8 to 15 candidates.
  `pick_next_region()` replaced with `pick_regions(db, vertical, n)`, same ranking logic
  (uncovered regions first, then oldest `last_searched_at`) but returns up to n regions
  in one pass, so there's no risk of repeating a region within the same run. `run()`
  loops up to 4 regions, updates `regions_covered` after each one (not just once), stops
  early once 20 qualified leads have been inserted this run, and now returns
  `{"regions_processed": [...], "leads_inserted": N, "ddg_failures": N}` instead of a
  plain int. Verified `pick_regions()` against real DB state (returns 4 distinct regions
  per vertical, correctly ranked).
- `leadgen.regions_covered` and `leadgen.daily_run_log` — new `ddg_failures integer not
  null default 0` column on each, since `lead_hunter.py` and `daily_summary.py` run as
  separate GitHub Actions steps (separate Python processes, no shared memory) — the
  in-process failure counter can't be handed to daily_summary directly, so it's persisted
  per-region and summed the same way daily_summary already sums `regions_covered_this_run`.
- `scripts/daily_summary.py` — sums today's `ddg_failures` across `regions_covered` rows,
  writes it to `daily_run_log`, and prints an explicit warning line if it's nonzero, so a
  DuckDuckGo throttling problem shows up directly instead of just reading as "fewer leads
  than usual" with no explanation.
- Both workflow YAMLs — schedule changed from one cron entry to four, 6 hours apart,
  staggered 2 hours between verticals: moto_apparel at 02/08/14/20 UTC, combat_sports at
  04/10/16/22 UTC. Validated both files with a real YAML parser — 4 entries each,
  confirmed correct.
- **Not verified**: a full live 4-region run (would burn significant real Groq/DDG call
  volume — up to ~4 regions x 16 searches x 1.5-6.5s pacing/retry each — without being
  requested). Each individual piece (region picking, DDG retry/pacing/failure-tracking,
  YAML syntax) was verified directly instead.

## 2026-07-21 — Cron investigation + pre-flight healthcheck step

Investigated why neither scheduled workflow had fired automatically yet. Root cause:
not a bug — the workflow files first landed on the default branch at 2026-07-20T10:39Z
(commit `49d45a5`), which is *after* both that day's cron times (06:00 and 09:00 UTC) had
already passed. Real UTC time as of this check was 2026-07-20T19:53Z, so neither cron slot
had had a genuine opportunity to fire yet — the first real opportunities are
2026-07-21T06:00Z (moto_apparel) and 2026-07-21T09:00Z (combat_sports), still hours out.
Confirmed via the GitHub web UI that Actions is enabled and neither workflow shows a
"disabled" state; cron syntax in both YAML files (`0 6 * * *` / `0 9 * * *`) is valid.
Note for future date references in this log: "today" here tracks Hamad's local calendar
day (Pakistan, UTC+5), not UTC — worth keeping in mind for anything cron-timing-related.

Also found, unrelated to the cron question: **the GitHub repo is public**, not private as
originally intended — confirmed by viewing full Actions run history from a logged-out
browser session. Flagged to Hamad directly; not something this session can fix (repo
visibility is an account-level setting, not something achievable via ordinary git push).

Added `scripts/healthcheck.py` — a pre-flight step (Groq call, Supabase query, Gmail OAuth
refresh) that now runs first in both workflows, right after `pip install`. Individual
pipeline stages intentionally swallow per-lead errors so one bad lead doesn't kill a whole
run, which means a fully invalid/expired credential could otherwise produce five green
checkmarks while silently doing nothing all day. The health check fails the job loudly and
immediately instead (verified locally: exits 1 and names the broken service when given a
deliberately invalid Groq key; exits 0 with all real credentials).

## 2026-07-21 — real_specs_combat_sports.json verified and activated

`config/real_specs_combat_sports.json` filled in with the real 3-model list (Heritage
Cotton-Blend, Pro-Stretch, Hygiene Kit) and `verified_by_hamad` flipped to `true` —
combat_sports Copywriter's deterministic template now goes live on the next run.

## 2026-07-21 — combat_sports Copywriter: deterministic template replacing open-ended generation

`scripts/copywriter.py` rewritten so combat_sports now builds a deterministic template
(fixed subject "Question regarding hand wraps stock", hardcoded 3-model list rendered
verbatim from `real_specs_combat_sports.json.materials`, a lookbook line that only appears
if `assets/catalogue/lookbook_hand_wraps.pdf` actually exists, fixed CTA) instead of
open-ended generation — the LLM is now scoped to writing ONLY the one-sentence opening
personalization line, so the three named models, subject, and CTA can never drift from
what's verified. moto_apparel is unchanged, still on the older open-ended
verified/unverified prompts pending its own spec verification.

**Not yet live**: `real_specs_combat_sports.json` still has `verified_by_hamad: false`
(placeholder materials), so the deterministic template's gate
(`vertical == "combat_sports" and verified`) doesn't trigger yet — combat_sports still
falls through to the same generic unverified path as before until Hamad fills in the real
3-model list and flips that flag. No `researched`-status combat_sports leads existed at
push time to run a live test against, and even if they had, the unverified specs file
means the live pipeline wouldn't have exercised the new path anyway. Verified the new code
works correctly instead via a standalone call to `build_combat_sports_email()` with example
(clearly non-production) specs — output pasted in the session for Hamad's sanity check.
Also confirmed the conditional lookbook line correctly stays absent, since no
`lookbook_hand_wraps.pdf` exists yet (per the prior session's search — none found in either
source folder).

## 2026-07-21 — Swap combat_sports catalogue from gloves to hand wraps

Replaced the active combat_sports product photo catalogue with hand wraps, sourced from
two local Google Drive folders (`Range 15` and `Range 18/ispo models`), reviewed by
actually opening and looking at each candidate image (the copywriter model that picks
images is text-only, so this is a one-time manual tagging pass, same approach as the
original glove catalogue).

- **Found**: 236 images total across both folders (46 in `Range 15`, 190 in
  `Range 18/ispo models` — 100 top-level RAW-named originals + 90 in an "Edited low
  resolution" subfolder). No PDF lookbook found in either folder — only `.jpg`/`.JPG`
  files plus one `Edited low resolution.rar` archive in `ispo models`, which by name and
  context appears to be a backup of the same edited images already reviewed there, not a
  separate lookbook (not extracted/opened, given no other signal it's anything else).
- **Reviewed**: `Range 15` turned out to be a dedicated hand-wrap product shoot — every
  image opened there showed the wraps themselves, their leather patch, or their
  packaging; zero gloves or unrelated products. `Range 18/ispo models` turned out to be a
  general mixed catalogue shoot instead — representative sampling spread across its full
  numbered range (01-57) and every distinct photo-session cluster in its `_MG_*` files
  (~14 images opened) showed boxing/MMA gloves, focus mitts, shin guards, headgear, and
  gi uniforms; zero hand wraps found. Not every one of the 190 ispo-models images was
  individually opened (impractical at that volume) — the conclusion rests on consistent
  sampling across every session cluster, not an exhaustive check.
- **Selected 8** (all from `Range 15`) into `assets/catalogue/combat_sports/`, replacing
  the glove images there: 4 colorways of the Heritage cotton-blend wrap with leather
  patch (red/green/blue/black — each a composite shot showing the rolled wrap, patch,
  and mesh pouch together), 2 angles of the red Pro-Stretch slip-on quick-wrap, and 2
  angles of the Hygiene Kit mesh carrying pouch. Originals copied at full quality, no
  recompression/resizing.
- **Excluded**: near-duplicate crop/retouch variants of the same 4 colorways (files with
  `-1`/`-2` suffixes — redundant with the composite hero shots already selected), and the
  entire `ispo models` folder (different product line — gloves/mitts/shin
  guards/gis, not wraps, per the sampling above).
- `config/catalogue_combat_sports_gloves.json` — the old glove catalogue, renamed/archived
  (git history preserved via `git mv`), no longer referenced by any code path.
- `config/catalogue_combat_sports.json` — new active file, 8 entries matching the
  filenames above, each description written from what's actually visible in that
  specific image (not reused generic text).
- No code changes needed: `copywriter.py`'s `load_catalogue()` builds the path as
  `f"catalogue_{vertical}.json"`, so it picked up the new file automatically. Verified by
  loading it directly — all 8 entries resolve correctly.
- The 9 old glove images in `assets/catalogue/combat_sports/` were left in place
  (untouched, not deleted) alongside the 8 new wrap images — only the JSON catalogue
  determines what the Copywriter can select from, so the old glove files are now
  effectively orphaned/unreferenced there. Flagging in case Hamad wants them removed for
  tidiness; left as-is since deleting wasn't asked for.

## 2026-07-20 — Add independent/boutique-scale disqualifier to verify prompts

Belstaff (moto_apparel) passed the brand-vs-manufacturer check and got inserted as a
lead, but it's a large heritage brand owned by a group, not a boutique/mid-size
independent brand — the actual target profile. Added an explicit size/independence
disqualifier to both `VERIFY_PROMPTS` in `scripts/lead_hunter.py`:

- `moto_apparel`: new step 3 disqualifies globally recognized heritage/luxury brands,
  brands owned by a large corporate/fashion group, or brands with mass-market retail
  distribution (reference scale: Belstaff, Alpinestars, Dainese are all TOO LARGE).
  `qualifies` now requires step 3 to pass too, and `one_line_reasoning` states explicitly
  if a candidate failed specifically on size/independence.
- `combat_sports`: new step 3 disqualifies large corporate-owned gym franchises/chains
  and big-box retail chains masquerading as a "shop" (existing sub_type classification
  step shifted to step 4). `qualifies` is false if step 3 fails regardless of other steps.

Stopgap until Gemini research on more reliable size-classification signals comes back.

## 2026-07-20 — Catalogue image attachments + per-vertical From address

- `outreach_leads.catalogue_images text[]` added. Copywriter now also picks 1-2 relevant
  product photo filenames per lead (text-only reasoning over pre-written descriptions,
  since Groq isn't vision-capable) from `config/catalogue_<vertical>.json`; Sender
  attaches the actual files from `assets/catalogue/<vertical>/`.
- 7 moto_apparel images (Anger-brand jackets + armored/mesh gloves) and 9 combat_sports
  images (MMA grappling gloves, several colorways/private-label variants) sourced from
  Hamad's real Google Drive catalogue and manually reviewed/tagged before inclusion.
- `gmail_client.create_draft()` now supports `attachment_paths` (MIMEMultipart +
  MIMEImage) and `from_email`. Verified live: `hm@relianmfg.com` and
  `hm@reliansports.com` are both confirmed working "send as" aliases on
  relianmfg@gmail.com — `sender.py` now sends moto_apparel drafts from the former and
  combat_sports from the latter instead of the raw Gmail address.
- Verified live end-to-end: image selection correctly matched a lead's specific detail
  (D30 Ghost armour → picked an armored-knuckle glove photo over a plain jacket), and a
  real test draft confirmed both the attachment and the custom From address landed
  correctly in Gmail.

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
