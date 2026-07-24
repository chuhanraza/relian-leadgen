# Changelog

## 2026-07-24 — combat_sports: hand-wraps banner now inline in the email body, not an attachment

Follow-up to the same-day banner change: Hamad asked for the banner to render inline in
the email itself (visible in the body, like an image in a normal HTML email) rather than
sitting as a separate downloadable attachment.

- **`scripts/common/gmail_client.py`**: `create_draft()` gained `inline_image_path` /
  `inline_image_cid`. When given, builds the standard Gmail inline-image MIME shape — a
  `multipart/related` part (the HTML body + the image with a matching `Content-ID` and
  `Content-Disposition: inline`) nested inside the outer `multipart/mixed`, so any real
  `attachment_paths` stay genuine separate attachments alongside it, not folded into the
  inline part. A plain-text body is auto-converted to safe, paragraph-preserving HTML
  (new `_plain_text_to_html()`, escapes special characters, blank-line breaks become
  `<p>` blocks) since a plain-text body can't carry an inline image; `is_html=True`
  callers (unaffected — eicma_campaign.py doesn't use inline images) keep their body
  as-is. A missing/unreadable inline image falls back to no image rather than failing
  the draft, same as attachment_paths.
- **`scripts/sender.py`**: replaced `EXTRA_ATTACHMENTS` with `INLINE_BANNERS` —
  combat_sports' `hand_wraps_banner.jpg` is now passed via `inline_image_path` instead of
  appended to `attachment_paths`. The 1-2 individually selected catalogue product photos
  are unchanged, still real attachments.
- **Live-verified** on a real Gmail draft (`boxen-babv.de` / Bayerische Boxer, German
  copy): read the actual draft back via the Gmail API — `multipart/related` containing
  the `text/html` body (with `<img src="cid:banner-image">` appended after the real
  paragraph text, paragraph breaks intact) and `hand_wraps_banner.jpg` as
  `Content-ID: <banner-image>`, `Content-Disposition: inline`; the 2 catalogue photos
  remained separate `Content-Disposition: attachment` parts, confirming the two don't
  get conflated. The 3 real drafts created earlier today with the old
  attachment-only banner (boxen-babv.de, buddhafightwear.com, khunpon.de) were deleted
  and regenerated with this fix so what's actually in Gmail now reflects it.

## 2026-07-24 — combat_sports: standing hand-wraps banner on every draft

- **`assets/catalogue/combat_sports/hand_wraps_banner.jpg`**: final approved banner
  (all 6 hand-wrap/case/bag product photos, dark background, "PRIVATE LABEL HAND WRAPS /
  Relian Sports" corner tag), 162,038 bytes (~158KB, well under the 300KB email-size
  budget), verified as a real baseline JPEG (1288x926).
- **`scripts/sender.py`**: added `EXTRA_ATTACHMENTS` (combat_sports only) — the banner is
  now attached to every outgoing draft in addition to the 1-2 individually selected
  catalogue photos, not instead of them. Attachment only, via the existing
  `gmail_client.create_draft()` MIMEImage path — no inline `<img>`, no change to the
  plain-text body copy.
- **Live-verified** against a real Gmail draft (not just code review): inserted an
  isolated, self-addressed test lead (`relianmfg@gmail.com`, clearly labeled
  `TEST BANNER VERIFICATION - DELETE ME`) since every real combat_sports lead was
  already either drafted-and-sent or not yet email-qualified. Ran `sender.py
  combat_sports` for real, then read the actual created draft's MIME parts back via the
  Gmail API: 3 parts — plain-text body, the 2 catalogue photos, and `hand_wraps_banner.jpg`
  at exactly 162,038 bytes (byte-for-byte matching the file on disk) as a standalone
  `image/jpeg` attachment. Test draft and test lead row deleted afterward.

## 2026-07-24 — Enricher/Copywriter per-run batch caps + real permanent-lockout bug fix

Diagnosed before assuming a cause: pulled the real GitHub Actions history for
combat_sports' 18:25 and 23:52 UTC (2026-07-23) runs. Both Enricher steps completed
with exit 0 (no crash, no timeout) in 9m41s and 2m2s. The 73 leads sitting at
status='researched' turned out to ALL already have non-null research_notes — every
single one was `[ENRICHMENT_FAILED] generate/parse failed twice: ... rate_limit_exceeded`
(confirmed via direct SQL: 73/73 rate-limit, 0 other causes, spanning all three of
that day's runs). So the actual problem was never "Enricher never got to them" or "one
run can't finish a 70+ backlog" — it's that combat_sports' Lead Hunter now makes far
more Groq calls per run (per yesterday's daily-target rewrite) and was burning through
the same Groq key's daily token budget before Enricher's own calls in the same job even
ran, so every Enricher attempt failed with a 429.

That alone would just mean "retry next run" — except **Enricher's failure path is not
retry-safe**: on a permanent research_notes write, the row is excluded forever from the
`research_notes IS NULL` query that finds pending work, since nothing distinguishes a
rate-limit hit (this run's fault) from a real content failure (this lead's fault). A
transient 429 was therefore permanently bricking leads with no path back to retry, even
after the daily token budget reset. This is the real bug — a per-run batch cap alone
would not have fixed it, since every lead in a capped batch would still hit the same
429 and still get permanently locked out.

- **`scripts/enricher.py`**: `ENRICHER_BATCH_SIZE = 40`, applied as
  `.order("created_at").limit(...)` on the pending query (oldest backlog first) so a run
  makes steady, bounded progress instead of attempting the whole backlog sequentially.
  Separately and more importantly: the double-failure handler now checks for
  `rate_limit_exceeded`/`429` in the exception text and, if found, leaves
  `research_notes` untouched (`continue` without writing) instead of writing a permanent
  `[ENRICHMENT_FAILED]` note — so a token-budget hit gets retried by a later run instead
  of being excluded forever. Non-rate-limit failures still get the permanent note
  (unchanged) since those are genuinely lead-specific, not run-timing-specific.
- **`scripts/copywriter.py`**: checked for the same missing-limit pattern —
  `COPYWRITER_BATCH_SIZE = 40` added the same way. Its existing failure path (bare
  `continue`, no notes touched) was already retry-safe, so no lockout bug there.
- **One-time data cleanup**: the 73 leads locked out by the bug above (all confirmed
  rate-limit-only failures via SQL, zero other-cause failures) had `research_notes`
  reset to `NULL` in `leadgen.outreach_leads` so they re-enter the real backlog instead
  of staying permanently stuck.
- **Live-verified** against production Supabase, real before/after (not estimated): ran
  `enricher.py combat_sports` against the real 73-lead backlog post-fix.
  `researched`+`research_notes IS NULL` (untouched backlog) went **73 → 33** (the
  40-cap processed cleanly, zero rate-limit errors on a fresh day's token budget); of
  those 40, 18 got a real found email (→ ready for Copywriter) and 22 landed on
  `skipped_no_email` (Facebook-only or nothing found). `drafted` unchanged at 20
  (Copywriter not run in this test).

## 2026-07-23 — combat_sports: daily-target-aware Lead Hunter (110 raw/day buffer)

`combat_sports` needs ~50 drafted leads/day; since roughly half of qualified leads never
yield a findable email, Lead Hunter now targets 110 raw-qualified leads/day as a buffer,
and keeps working until that's actually met instead of stopping at a fixed per-run cap.
moto_apparel's sizing (`MAX_REGIONS_PER_RUN=2` / `QUALIFIED_TARGET_PER_RUN=20`) is
unchanged.

- **`scripts/lead_hunter.py`**: combat_sports now runs `_run_daily_target()` — queries
  today's (UTC) `outreach_leads` count for the vertical at the start of every run,
  computes `remaining = 110 - today's_count`, exits immediately if already met, otherwise
  loops full passes over all 17 regions (varying discovery query phrasing pass-to-pass so
  it's not repeating identical searches) until remaining is satisfied or 3 full passes
  complete with zero new candidates in the last pass (the real "today's supply is
  exhausted" signal — logged explicitly). Each cron slot re-checks the cumulative count
  rather than assuming a fresh day, so the existing 4x/day schedule now naturally tops up
  whatever's still short instead of risking a short run from a DuckDuckGo failure storm.
  Logs today's-count-before, remaining, pass count, and final outcome every run.
- **Bugfixes surfaced by actually running the new loop at volume** (the old 2-region cap
  rarely exercised these paths enough to hit them): `discover_names()` crashed with
  `AttributeError` when the fast model replied with a JSON array of bare strings instead
  of `{"brand_name": ...}` objects — now skips malformed entries instead of dying mid-run;
  `verify_candidate()` accepted the literal string `"null"` as a domain (model returning
  text instead of JSON null) and inserted a garbage row — now rejected the same as a
  missing domain. One garbage row (`brand_name="Warrior Gym"`, `domain="null"`) inserted
  during testing was deleted from `leadgen.outreach_leads`.
- **Live-verified** against the real `leadgen.outreach_leads` table: today's count went
  7 → 47 (first test run, before the bugfixes above were found) → 59 (second run, with
  fixes, `today_count_before=47`, `remaining=63`, 3 passes, `outcome=exhausted`, 12 new
  inserted). The second run's exhaustion was a real Groq daily-token-budget hit (500K TPD)
  on the local dev key from two large back-to-back test runs today — not a code fault. In
  production, combat_sports' 4x/day cron runs against its own dedicated
  `GROQ_API_KEY_COMBAT_SPORTS` with its own 500K TPD headroom, unaffected by this local
  testing.

## 2026-07-23 — EICMA invitation campaign (new system, separate from cold outreach)

Adds a standing "wave" invitation campaign for EICMA 2026 (Nov 5-8, Hall 14 Booth A10),
independent of the moto_apparel/combat_sports cold-outreach pipeline. Sends up to 20
Gmail drafts/day from `leadgen.eicma_invitations` (242 contacts, 1 suppressed), cycling
through non-suppressed contacts and rotating to a new wave design (new subject + HTML)
each time a full pass completes.

- **Schema**: `leadgen.eicma_campaign_state` (single-row wave counter, RLS enabled, no
  policies — service-role only, same pattern as every other table in this schema).
- **`config/eicma_waves/`**: `wave_1.html` (dark navy header w/ `assets/branding/
  relian-logo.png`, hero jacket photo, event details block, orange CTA), `wave_1.json`
  (subject line), and a README documenting the `{{GREETING}}` token convention and how to
  add wave_2+ before the campaign needs it (falls back to the highest available wave and
  logs `NEEDS NEW WAVE DESIGN` rather than failing if the next one isn't ready).
- **`scripts/eicma_campaign.py`**: per-contact greeting extraction via Groq
  `llama-3.1-8b-instant` (same fast model as `discover_names`/`select_images`), never-
  contacted-first / oldest-`last_sent_at`-next ordering, `suppressed=true` always
  excluded from the query. Hard rule unchanged from the rest of this repo: only
  `drafts().create`, never `messages().send`.
- **`scripts/common/gmail_client.py`**: `create_draft()` gained an `is_html` flag (default
  `False`, so the existing plain-text cold-outreach callers are unaffected) to support
  the fully-designed HTML wave templates.
- **`.github/workflows/eicma_campaign.yml`**: daily cron at 12:00 UTC (moto_apparel runs
  at 2/8/14/20:00, combat_sports at 4/10/16/22:00 — no collision).
- Live-verified: ran a real batch of 20 against the actual `eicma_invitations` table —
  correct exclusion of the 1 suppressed contact, correct greeting extraction (e.g.
  "Spyke" from `info@spyke.it`), 20 real Gmail drafts created (never sent), wave counter
  and per-contact `last_sent_at`/`status`/`last_wave_design` updated correctly.

## 2026-07-23 — 5 new regions per vertical, Brazilian Portuguese template + icebreaker

Adds coverage for regions Lead Hunter had no vetted path into, plus a full `pt` localization
for `combat_sports` (the first non-Romance-strict-formal language in the set — see note below).

- **`scripts/common/regions.py`**: added `Eastern Europe`, `Southeast Asia (ex-China)`,
  `Brazil`, `Latin America (ex-Brazil)`, `South Africa` to `MOTO_APPAREL_REGIONS`; added
  `Brazil`, `Latin America (ex-Brazil)`, `South Africa` to `COMBAT_SPORTS_REGIONS`
  (`Eastern Europe`/`Southeast Asia (ex-China)` were already present there). Renamed the
  existing `combat_sports` region `"Latin America"` to `"Latin America (ex-Brazil)"` to
  split out Brazil as its own (now Portuguese-routed) region — since this is a rename, not
  a fresh addition, the matching `leadgen.regions_covered` row was migrated in place
  (`UPDATE ... SET region = 'Latin America (ex-Brazil)'`) so its `last_searched_at` history
  (2026-07-21) carries over instead of the region looking falsely "never searched" and
  getting needlessly re-scraped. No `outreach_leads` rows existed under the old region
  string, so nothing else needed relabeling.
- **`LANGUAGE_BY_REGION`**: added `Brazil -> pt`. Confirmed `language_for_region()` is
  only ever called from `copywriter.py`'s `vertical == "combat_sports"` branch — `moto_apparel`
  never routes through it — so `moto_apparel`'s English-only behavior is unaffected even
  though `Brazil` is now in both verticals' region lists.
- **`config/email_templates_combat_sports.json`**: added a `pt` entry, same schema as
  `en`/`de`/`fr`/`es`/`it`.
- **`scripts/copywriter.py`**: added `pt` to `ICEBREAKER_PROMPTS` and `FORBIDDEN_INFORMAL`
  (targets marketing-hyperbole terms — "revolucionário", "incrível oferta" — instead of a
  T-V pronoun split, since Brazilian Portuguese uses "você" as the standard professional
  register and doesn't have the same formal/informal divide as the other four languages).
  Also added a `pt` entry to `FALLBACK_ICEBREAKERS`: `generate_icebreaker()` does a bare
  `FALLBACK_ICEBREAKERS[lang]` lookup with no default, so a Portuguese lead whose two
  generation attempts both failed validation would have hit a `KeyError` mid-run without
  this — added to keep parity with the other 4 languages.
- Brand-new regions have zero history in `leadgen.regions_covered`, so Lead Hunter's
  existing least-recently-searched rotation will naturally prioritize them without any
  extra logic.

## 2026-07-23 — Reject discovered emails belonging to a different business

`scripts/common/contact_discovery.py` gained `email_matches_business()` / `_domain_root()`
/ `GENERIC_EMAIL_PROVIDERS`, wired into all three email-acceptance points in
`scripts/enricher.py` (primary extraction, secondary-page crawl, waterfall fallback).
Fixes a confirmed live bug where a name-based search step (Facebook dork, Instagram,
Apollo) surfaced a plausible-sounding but wrong email for a common/generic business name —
e.g. `united-fightwear.com` got assigned `okami-fightgear.com`'s email, and two unrelated
"Invictus Fight Academy" gyms in different countries got each other's contacts. A
discovered email is now accepted only if its domain root matches the lead's own domain, or
(for generic providers like gmail) its local-part shares a brand-name token — otherwise
it's rejected and logged, falling through to the next waterfall stage exactly like "no
email found." The 3 known-bad leads were reverted to `status='researched'` and
re-processed through `enricher.py combat_sports`: `united-fightwear.com` and
`invictusacademy.com.tr` correctly landed on `skipped_no_email` (their previous wrong
emails were rejected, nothing valid replaced them); `invictusmartialarts.com` found a
new, validated email.

## 2026-07-21 — Deeper own-site crawl, Apollo last-resort stage, directory batch ingestion

Extends the existing `scripts/common/contact_discovery.py` waterfall with higher-yield
stages, plus a separate seed-list ingestion path. No parallel/second discovery module —
everything lives in the one file, same as before.

- **Secondary-page/sitemap crawl** (`crawl_domain_secondary_pages()` in
  `contact_discovery.py`, wired into `enricher.py`): before falling through to
  Overpass/Facebook-dork/Instagram, Enricher now re-checks the lead's OWN known domain
  more thoroughly — parses `/sitemap.xml` (stdlib `xml.etree.ElementTree`, not
  BeautifulSoup's lxml-backed XML mode, to avoid adding an lxml dependency) for
  contact/about/wholesale-type URLs, falls back to homepage internal links if there's no
  sitemap, and checks up to 5 pages for a `mailto:` link or a body-text email match. This
  runs BEFORE the Overpass/FB/Instagram waterfall since it's re-examining an
  already-known source, not trying a new one. Added `beautifulsoup4` to
  `requirements.txt` for HTML parsing (stdlib `html.parser` backend, still no lxml).

- **Apollo.io, last resort, verified-emails-only** (`try_apollo()` in
  `contact_discovery.py`, wired as the final stage of `discover_contact()`, after
  Instagram/Linktree): organization search -> people search -> people match against
  Apollo's real REST API. Hard rule: only accepts an email when Apollo's own
  `email_status` is exactly `"verified"` — `"likely to engage"`, `"unverified"`,
  `"unavailable"`, or anything else is rejected the same as no match, no exceptions.
  This needs its own `APOLLO_API_KEY` (repo secret + local `.env`) — it calls Apollo's
  REST API directly, which is a different credential from any Apollo MCP connector,
  since the unattended GitHub Actions cron can't reach an interactive session's MCP
  connection. It's optional and off by default; the pipeline runs unchanged without it.
  Because each attempt can spend real Apollo credits and this runs unattended 4x/day per
  vertical with no per-call confirmation, added a hard `APOLLO_MAX_CALLS_PER_RUN`
  env-configurable cap (default 5, enforced **per vertical per Enricher run** — both
  workflows share one Apollo key/cap, so a full day is effectively up to
  `cap × 4 runs × 2 verticals` attempts, not a single global daily cap). Every attempt's
  outcome (`not_configured`, `cap_reached`, `no_org_found`, `no_people_found`,
  `rejected_unverified:<status>`, `verified_email_found`) is appended to
  `research_notes` so Hamad can see the real hit rate after a week and decide if it's
  worth the credit cost at all.

- **Directory/trade-show batch ingestion** (`scripts/ingest_directory_list.py`, new,
  separate from the daily per-lead waterfall): takes a CSV path or URL with columns
  `brand_name, region, email (optional), website (optional)` — Hamad supplies the actual
  exhibitor-list export (EICMA/ISPO/Europages etc.), since these change yearly and need a
  human to find the current one. Inserts as `status='researched'` leads, reusing Lead
  Hunter's dedup-by-domain logic so re-running against an updated export is idempotent.
  Rows with a real email are marked already-enriched (`research_notes` set, so Enricher's
  `research_notes IS NULL` filter skips them); rows without one are left for Enricher to
  research normally. Rows with neither a website nor an email are skipped (not inserted
  with a fabricated domain), since `outreach_leads.domain` is `NOT NULL`.

**Deferred, not built**: a headless-browser (Playwright + Chromium) Google Maps scrape,
scoped for brick-and-mortar leads (gyms/shops) where every existing method — including
the two above — finds no website at all. Skipped for now because it's a materially
heavier CI dependency (large install, slower/more fragile in GitHub Actions than the
plain-`requests` stages) and scrapes a live Google product's public results, which
carries similar (if milder) ToS tension to the Facebook dork. Revisit only after
confirming the two stages above actually move the needle on the real gap, specifically
for brick-and-mortar leads rather than online brands.

## 2026-07-21 — moto_apparel deterministic template, English only

Brings moto_apparel to parity with combat_sports's approach: real verified specs +
a deterministic template, with the LLM scoped to ONLY the one-sentence icebreaker.
No multilingual work here — that stays scoped to combat_sports for now.

- **`config/real_specs_moto_apparel.json`** replaced with Hamad's verified specs:
  leather (Premium Natural Leather, hide-inspection process), two value props
  (ERP-integrated tracking, transparent/open costing), and two certifications (SGS
  Approved Report No. 151756704; Alibaba Verified/Trustpass). `verified_by_hamad`
  flipped to `true`.

- **`config/email_templates_moto_apparel.json`** (new): single English entry —
  subject, greeting, philosophy line, CTA, signoff. Deliberately a flat structure,
  not the per-language shape `email_templates_combat_sports.json` uses — moto_apparel
  has exactly one language, so there's no per-language dict to merge into.

- **`scripts/copywriter.py`**: added `build_moto_apparel_email()`, following the
  same pattern as `build_combat_sports_email()` — a new `MOTO_ICEBREAKER_PROMPT`
  generates only the opening sentence, run through the same `validate_icebreaker('en', ...)`
  firewall already guarding combat_sports's English icebreaker, so an off-register or
  hallucinated line never ships. Everything else (philosophy, leather paragraph,
  value props, certifications mention, CTA, signoff) is rendered straight from config
  — no LLM involvement, no drift from what's verified. `run()` now dispatches to it
  whenever `vertical == "moto_apparel"` and `verified_by_hamad` is `True`, instead of
  the old open-ended `PROMPT_VERIFIED` path. `PROMPT_VERIFIED`/`PROMPT_UNVERIFIED`
  stay in place as the defensive fallback for a vertical without its own template yet,
  or an unverified spec — kept for the (now impossible, but worth guarding) case where
  `verified_by_hamad` flips back to `false`.

- **Catalogue**: no change needed — `config/catalogue_moto_apparel.json` and the 7
  real glove/jacket photos in `assets/catalogue/moto_apparel/` already exist from the
  2026-07-20 build (unlike combat_sports at that point, moto_apparel's photos were
  already in hand); `load_catalogue()`'s existing missing-file handling was never
  exercised here.

## 2026-07-21 — Split Groq key per vertical, cheaper model for low-stakes calls

The two verticals were competing for one shared Groq free-tier token budget (100K TPD),
throttling both when either ran hot.

- **Per-vertical Groq key**: Hamad manually created a second free Groq account and added
  its key as the GitHub secret `GROQ_API_KEY_COMBAT_SPORTS`. `combat_sports.yml` now maps
  `GROQ_API_KEY` to that secret instead of the original `GROQ_API_KEY`; `moto_apparel.yml`
  is untouched and keeps drawing from the original key. `scripts/common/groq_client.py`
  needed no change — it already just reads `os.environ["GROQ_API_KEY"]`, and each workflow
  now injects a different real key under that same env var name. Each vertical now has its
  own 100K TPD pool instead of splitting one, roughly doubling the system's effective daily
  budget.

- **Cheaper model for low-stakes extraction calls** (`scripts/common/groq_client.py`):
  `generate()` now takes a `model` parameter (`MODEL_QUALITY` = `llama-3.3-70b-versatile`,
  default; `MODEL_FAST` = `llama-3.1-8b-instant`, far more generous free tier — 14,400 RPD
  / 500K TPD vs 1,000 RPD / 100K TPD). Switched to `MODEL_FAST`: `lead_hunter.py`'s
  `discover_names()` (raw name extraction from search results) and `copywriter.py`'s
  `select_images()` (picking 1-2 filenames from a short list). Left on the default 70B
  model, where output quality genuinely matters: `verify_candidate()`'s brand/size/type
  judgment call, `generate_icebreaker()`'s actual email content (already guarded by
  `validate_icebreaker()`), and Enricher's research/email-extraction call.

- Added a safe key fingerprint (`...last 4 chars`) to `healthcheck.py`'s Groq check so a
  manually triggered run's log makes it obvious which of the two keys was actually used,
  without ever printing the full key.

## 2026-07-21 — Contact-discovery waterfall, Facebook Pages as valid targets, DE/FR/ES/IT templates

Three additions, all additive to the existing pipeline:

- **Contact-discovery waterfall** (`scripts/common/contact_discovery.py`): when Enricher's
  primary route (homepage fetch + DDG search + Groq extraction) finds no email, it now
  falls back through OpenStreetMap Overpass API (free, no key) → a DuckDuckGo
  `site:facebook.com` dork that regexes any published email out of indexed snippet text
  (never fetches facebook.com directly — it's a login wall) → an Instagram-bio search that
  follows an exposed Linktree/Beacons link if one appears (those are plain static pages,
  safe to fetch). Every stage is best-effort and falls through silently on failure; the
  waterfall still never invents a pattern email — `discover_contact()` returns `email: None`
  if nothing real was found, same contract as the primary route. If the waterfall finds a
  Facebook Page but no email, the lead now gets `contact_method='facebook_message_manual_needed'`
  (a new value, distinct from the existing `contact_form_manual_needed`) with the page URL
  saved to both the new `facebook_url` column and appended to `research_notes`, so Hamad's
  team can go message the page directly instead of the lead being an undifferentiated
  "skipped, no way to reach them."

- **Facebook Pages accepted as Lead Hunter targets** (`scripts/lead_hunter.py`): previously
  a candidate with no discoverable official website was disqualified outright.
  `verify_candidate()` now accepts a clearly-matching Facebook business Page as the
  candidate's online presence — `domain` becomes `facebook.com/<pagename>`, `website_url`
  the full Facebook URL — and never calls `fetch_page_text()` on a facebook.com result
  (login wall, would just poison the prompt with garbage). The verify prompt for both
  verticals now explicitly bases steps 2+ on the DDG search snippets alone when working
  from a Facebook Page match, since there's no fetchable homepage text for it.

- **Real multilingual combat_sports templates — German, French, Spanish, Italian only**
  (`config/email_templates_combat_sports.json`): `scripts/common/regions.py` split the
  mixed-language "Western Europe"/"Southern Europe" region groupings into single-language
  regions (`Germany/DACH`, `France/Benelux`, `Spain`, `Italy`) so `language_for_region()`
  can route precisely instead of guessing across a mixed grouping. Every other region
  (Nordics, Eastern Europe, Latin America, Middle East, etc.) deliberately still defaults
  to English — there is no vetted template for them yet, and none should be added without
  a real one backing it. `copywriter.py`'s `build_combat_sports_email()` now renders
  subject/greeting/intro/models/cta/signoff entirely from the per-language template
  (`target_language` computed via `language_for_region(lead['region'])` at draft time and
  persisted to the new `outreach_leads.target_language` column); the English-only Lookbook
  attachment mention is intentionally not carried into the untranslated languages.
  Icebreaker generation is now language-aware (`ICEBREAKER_PROMPTS` per language, formal
  register enforced in the prompt itself) and every generated icebreaker — in every
  language, including English — passes through a `validate_icebreaker()` firewall before
  use: rejects anything too short/long, any hallucinated Cyrillic/CJK/Japanese script, and
  (for DE/FR/ES/IT) any informal "du/tu/tú" register leakage. One retry on failure, then a
  fixed safe fallback line per language (logged to stdout so the fallback rate per language
  is visible in run output) — never an unvalidated line goes out.

  **Follow-up before high-volume sending in these languages:** the DE/FR/ES/IT templates
  and icebreaker prompts are AI-drafted, not yet reviewed by a native speaker. Recommend a
  native-speaker pass before scaling volume, same as `real_specs_combat_sports.json`
  required Hamad's own verification before its English content shipped.

  Migration `leadgen_social_contact_discovery_and_target_language` (applied directly via
  Supabase MCP against `iuhdisnbmksoxdeumhpf`, no local migration file — same convention
  as this repo's prior schema changes) added `target_language text not null default 'en'`
  and `facebook_url text` to `leadgen.outreach_leads`, and widened
  `outreach_leads_contact_method_check` to include `'facebook_message_manual_needed'`.

## 2026-07-21 — Scale back Lead Hunter per-run volume to fit the shared Groq token budget

Following the prior entry's finding (4 regions x 15 candidates consumed ~99.5k of Groq's
shared 100k-tokens/day free budget in a single vertical's single run), Hamad chose to scale
back per-run volume rather than accept uneven/throttled days or cut runs/day. `verify_candidate()`
(the per-candidate domain-resolution call, with page-text context — the dominant token
cost at up to 60 calls/run before this change) is the main thing this reduces.

- `MAX_REGIONS_PER_RUN`: 4 → 2.
- `discover_names()` candidate cap: 15 → 10 (roughly back toward the original 8, slightly
  higher). Worst case this brings verify calls/run from up to 60 down to up to 20 — about
  a 3x reduction in the dominant cost driver.
- 4 runs/day per vertical unchanged, per Hamad's choice to keep run frequency and reduce
  per-run depth instead.
- `discover_names()` also hardened the same way `copywriter.py` was fixed in the prior
  entry: its `generate()` call had no error handling, so a Groq failure there would have
  crashed `lead_hunter.py` entirely and skipped every remaining region plus every later
  stage (Enricher/Copywriter/Sender/Summary) for that run — the exact same crash-cascade
  bug, just not yet triggered on this code path. Now logs and returns `[]` for that region
  (treated the same as "found nothing"), letting the run continue.
- README updated to reflect the new 2x10 numbers and cite the real token-usage evidence
  for why, instead of restating the original untested 4x15 estimate as current behavior.

Real per-day sustainable throughput at 2x10x4 still isn't confirmed — no token-usage
telemetry is exposed by Groq outside of the rate-limit error message itself, so this is a
reasoned estimate (roughly 1/3 of the volume that exhausted the entire daily budget in one
run), not a guarantee. May need another round of tuning once a few real days of data come in.

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
