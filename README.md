# Relian MFG — Lead Gen (moto_apparel + combat_sports)

Unattended, daily B2B lead-gen for two verticals separate from the existing glove-buyer
pipeline:

- **moto_apparel** — boutique/mid-size motorcycle technical apparel brands (USA, Canada,
  Europe, UK, Australia, Japan)
- **combat_sports** — boxing/MMA/Muay Thai/BJJ gyms, shops, and small private brands
  (Mainland China explicitly excluded)

**Emails are never auto-sent.** The pipeline creates Gmail drafts only; you review and
send manually each morning. This is a permanent design constraint, not a placeholder — see
"No auto-send" below before changing that.

## Architecture

Five stages, one script each, run in order by the two GitHub Actions workflows
(`.github/workflows/moto_apparel.yml`, `combat_sports.yml`, staggered 6am/9am UTC cron):

1. `scripts/lead_hunter.py` — rotates through regions (least-recently-covered first, see
   `scripts/common/regions.py`), runs a free DuckDuckGo search and hands the results to
   Groq (free LLM, no card) to extract candidate leads, dedups by `(vertical, domain)`,
   inserts as `status='researched'`.
2. `scripts/enricher.py` — for each researched lead, pulls 1-2 genuine specifics from
   their site/social and looks for a real published email. No email found →
   `status='skipped_no_email'`, stops there (never proceeds to Copywriter).
3. `scripts/copywriter.py` — drafts a short pitch referencing the actual research detail.
   Technical claims come only from `config/real_specs_<vertical>.json`, only if
   `verified_by_hamad: true` (see below). Also picks 1-2 relevant product photos from
   `config/catalogue_<vertical>.json` (see "Catalogue images" below).
4. `scripts/sender.py` — creates a Gmail **draft** (never sends) for each drafted lead,
   from the vertical's own alias (`hm@relianmfg.com` for moto_apparel,
   `hm@reliansports.com` for combat_sports — both confirmed verified send-as aliases on
   relianmfg@gmail.com), with the Copywriter's picked photos attached.
5. `scripts/daily_summary.py` — logs the day's counts to `leadgen.daily_run_log`.

Data lives in a new `leadgen` schema inside the existing **relian-erp** Supabase project
(`iuhdisnbmksoxdeumhpf`) — not a separate project. The org's Supabase account is capped at
2 free active projects (already used by `relian-erp` and `Visit Plane 1 F`), so this reuses
`relian-erp` with its own isolated schema instead: `leadgen.outreach_leads`,
`leadgen.regions_covered`, `leadgen.daily_run_log`. RLS is enabled with zero policies, so
only the `service_role` key (used by these scripts) can read/write it — anon/authenticated
roles get nothing, and none of the ERP's own `public.*` tables were touched.

## Setup — everything here is free, no billing/card on any service

1. `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
2. `cp .env.example .env` and fill in:
   - `GROQ_API_KEY` — free key, no card required ever, from
     [console.groq.com/keys](https://console.groq.com/keys)
   - `SUPABASE_SERVICE_ROLE_KEY` — relian-erp project → Settings → API Keys → Secret keys
     (never commit this, never expose it client-side)
   - Gmail credentials — see below

Note: web research (Lead Hunter, Enricher) is done with a free DuckDuckGo search
(`ddgs` package, no API key at all) plus direct page fetches, then handed to Groq to
extract structured results. This was chosen over Claude/Gemini specifically because both
of those gate their free quotas behind a linked billing card, even at $0 actual usage —
Groq's free tier requires no card, full stop. Tradeoff: it's grounded in search snippets
and fetched page text rather than an AI actively browsing multiple pages per query, so
research quality is a notch below a paid tool-use model — it still never fabricates an
email or fact, it just has a smaller window to find one in per lead.

### Gmail API setup (one-time, must be done by Hamad)

I can't complete Google's OAuth consent for you — it has to be approved by you, in your
own browser, logged into `relianmfg@gmail.com`. Steps:

1. Go to console.cloud.google.com, create a project (or reuse one), enable the **Gmail
   API**.
2. Credentials → Create Credentials → OAuth client ID → Application type **Desktop app**.
   Copy the client ID and client secret into your local `.env` as `GMAIL_CLIENT_ID` /
   `GMAIL_CLIENT_SECRET`.
3. Run `python scripts/get_gmail_refresh_token.py` locally. It opens a browser consent
   screen — sign in as `relianmfg@gmail.com` and approve. It prints a refresh token.
4. Add all six as GitHub repo secrets (Settings → Secrets and variables → Actions):
   `GROQ_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GMAIL_CLIENT_ID`,
   `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`.

Until these secrets exist, both workflows will fail at the API-call steps — that's
expected; trigger a manual run (`workflow_dispatch`) once they're set to confirm.

## Filling in the spec config — REQUIRED before real technical claims go out

`config/real_specs_moto_apparel.json` and `config/real_specs_combat_sports.json` are
placeholders:

```json
{
  "materials": [{ "name": "PLACEHOLDER", "verified": false, "notes": "" }],
  "certifications_or_benchmarks": [],
  "moq_and_lead_time": "",
  "verified_by_hamad": false
}
```

**While `verified_by_hamad` is `false`, the Copywriter will never mention a specific
material, certification, MOQ, or lead time.** It drafts a generic capability statement
instead and appends "AWAITING SPEC VERIFICATION" to that lead's `research_notes` so it's
visible in review. This is intentional — it stops the pipeline from inventing technical
claims about your own manufacturing.

To unlock real technical claims: fill in `materials` (e.g. actual leather/synthetic/
laminate specs you use), `certifications_or_benchmarks`, `moq_and_lead_time`, then set
`verified_by_hamad: true`. Only you can flip that flag — it's a deliberate manual gate.

## Catalogue images

`config/catalogue_moto_apparel.json` and `config/catalogue_combat_sports.json` each list
the product photos in `assets/catalogue/<vertical>/` with a short description (written by
looking at each photo once — the text model that drafts emails can't see images). For
every lead, the Copywriter asks Groq to pick the 1-2 most relevant filenames from that
list based on the lead's `research_notes`, stores them in `catalogue_images`, and the
Sender attaches the actual files. Sourced from Hamad's real Google Drive catalogue
(`Cataloge/Anger Volume` for moto_apparel — "Anger" is Relian's own motorcycle brand —
and `Cataloge/MMA` for combat_sports). To add more: drop a jpg in the right folder, add a
`{"filename": ..., "description": ...}` entry describing what's actually in it, done — no
code changes needed. Keep individual files well under ~5MB; Gmail rejects attachments
whose total encoded size exceeds ~25MB.

## Sender reconciliation — known tradeoff

Once a lead is drafted, `status` stays `'drafted'` **permanently** — there's no automatic
check against your Gmail Sent folder to flip it to `'sent'`. This was the simplest option
to implement reliably (a Sent-folder-matching job adds real complexity for a purely
cosmetic status field). Idempotency against re-drafting is handled separately: the Sender
only acts on leads where `gmail_draft_id IS NULL`, so a lead only ever gets one Gmail draft
created, regardless of how many days `status` sits at `'drafted'`. You reconcile "did I
actually send this" by looking at Gmail itself, not this table.

## No auto-send

The Sender stage only ever calls Gmail's `drafts.create`, never `messages.send`. If a
future request asks to "just send them automatically," treat this as still the intended
design unless there's an explicit new instruction reversing it — flag it rather than
silently building around it.

## Manual runs / testing

Each script takes the vertical as its only argument and can be run standalone once `.env`
is filled in, e.g. `python scripts/lead_hunter.py moto_apparel`. Stages are independent —
you can inspect `leadgen.outreach_leads` after each one before letting the next run.
