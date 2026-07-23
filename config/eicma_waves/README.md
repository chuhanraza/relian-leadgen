# EICMA wave designs

Each wave is a standalone email design, numbered so the campaign can rotate to a fresh
subject/design every time it completes a full pass over the non-suppressed contact list.

- `wave_N.html` — full standalone HTML email. Must contain the literal token `{{GREETING}}`
  somewhere in the body (typically `Dear {{GREETING}},`) — `scripts/eicma_campaign.py`
  substitutes this with the per-contact extracted greeting before creating the Gmail draft.
  Image `src` values must be absolute URLs (e.g. `raw.githubusercontent.com/...`) since Gmail
  won't inline local file paths.
- `wave_N.json` — `{"subject": "..."}` for that wave.

## Adding a new wave

Once `leadgen.eicma_campaign_state.current_wave` advances past the highest numbered pair on
disk (i.e. a full pass just completed and there's no `wave_(N+1).html`/`.json` yet), the
script keeps using the highest available wave and prints a `NEEDS NEW WAVE DESIGN` notice
in the run log instead of failing. Add `wave_(N+1).html` + `wave_(N+1).json` before the next
scheduled run to have the new wave picked up automatically — no code change needed.
