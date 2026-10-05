# EICMA wave designs

Each wave is a standalone email design, numbered so the campaign can rotate to a fresh
subject/design every time it completes a full pass over the non-suppressed contact list.

- `wave_N.html` — full standalone HTML email. Must contain the literal token `{{GREETING}}`
  somewhere in the body (typically `Dear {{GREETING}},`) — `scripts/eicma_campaign.py`
  substitutes this with the per-contact extracted greeting before creating the Gmail draft.
  Image `src` values must be absolute URLs (e.g. `raw.githubusercontent.com/...`) since Gmail
  won't inline local file paths.
- `wave_N.json` — `{"subject": "..."}` for that wave.

## Rotation (changed 2026-10-06)

Waves no longer rotate on a full-list pass — that loop is removed. Each contact's wave follows
their own `cycle_number` (0 -> wave_1, 1 -> wave_2, 2 -> wave_3), capped at 3 drafts and
14 days apart (see `scripts/eicma_campaign.py`). `wave_4` is the single final reminder: it is
only used when `EICMA_FINAL_REMINDER=1` is set and is not picked up by the regular run.
Missing wave files are an error; there is no fallback to the highest wave on disk.
