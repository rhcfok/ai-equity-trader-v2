# UW GEX

Unusual Whales–backed unsigned gamma-exposure (GEX) workflow.

## Contents

- `SKILL.md` — operating instructions, configuration, and unsigned-GEX rules.
- `scripts/uw-gex_workflow_runner.py` — loads the live Supabase `watchlist` categories `core`, `satellite`, and `watch1`; fetches Unusual Whales contract gamma/open-interest data; and writes append-only `gex_snapshot` rows.
- `references/methodology.md` — calculation and non-claim methodology.
- `tests/test_uw_gex_workflow_runner.py` — offline regression coverage for source selection, filtering, retry behavior, expiry selection, contract parsing, and unsigned aggregation.

## Defaults

The runner selects explicitly US-listed symbols from `public.watchlist`, ordering categories as `core`, `satellite`, then `watch1`, and deduplicating symbols. `BRIEFING_SYMBOLS` is reserved for an intentional one-off override.

## Validation

```bash
python3 skills/uw-gex/tests/test_uw_gex_workflow_runner.py
python3 -m py_compile skills/uw-gex/scripts/uw-gex_workflow_runner.py
```

The workflow computes unsigned call- and put-side magnitudes only. It does not infer dealer long/short gamma.
