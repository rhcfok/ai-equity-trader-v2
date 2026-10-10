---
name: jlaw-v2-check
description: Post-run quality-control audit for the J Law v2 pipelines. Use after jlaw-v2-yahoo and/or jlaw-v2-chart runs to verify Supabase trade-jlaw-v2 results — coverage vs the watchlist, review-type labels, null audits, value sanity checks, score-band consistency, and cross-pipeline agreement. Also use when results look wrong (null cells, glitched prices, unexpected labels) to diagnose which pipeline/runner caused it.
icon: shield-check
color: Green
---

# J Law v2 Result Check (QC layer)

This skill is the **quality-control layer** of the J Law v2 workflow. It
does not produce research — it audits what `jlaw-v2-yahoo` and
`jlaw-v2-chart` wrote into the Supabase `trade-jlaw-v2` table and reports
defects before anyone trades on the output.

Run it after every pipeline run, on either machine. It is read-only by
default; the only write it can perform is label normalization
(`--normalize-labels`), and only when explicitly requested.

> Research tooling only. No orders, no brokerage connection, no
> personalized financial advice.

## What it checks

1. **Coverage** — row counts per `run_date × jlaw_review_type`, compared
   against the `watchlist` table: yahoo rows should cover the **entire**
   watchlist; chart rows should cover `category ∈ {core, satellite, watch1}`.
   Missing symbols are listed (capped) so a partial run is caught.
2. **Labels** — any `jlaw_review_type` outside `{yahoo, chart}` (the
   historical bug: a runner wrote `"data"`). With `--normalize-labels`,
   non-chart labels are rewritten to `yahoo`.
3. **Null audit** — null counts for `jlaw_score`, `entry_price`,
   `current_price`, `stop_loss`, `classification`, split by review type.
   Chart rows with `classification = 'Unreviewed'` are exempt from the
   score-null finding (nullable by design); every other null is a defect.
4. **Sanity** — `entry_price` outside `[0.5×, 2×] current_price`
   (misplaced-percentage bug), `stop_loss` above `1.5× current_price`,
   `stop_pct` negative or > 25, `rrr` < 0.
5. **Score-band consistency** — `classification` vs `jlaw_score` band
   (0–9 Skip · 10–12 Watch · 13–14 Valid · 15–16 A+). Mismatches usually
   mean the runner hand-set a classification.
6. **Cross-pipeline agreement** — for symbols covered by both pipelines on
   the same date, compare classifications and scores; list conflicts
   (e.g. yahoo Valid vs chart Skip — remember: a failing chart overrides
   a flattering score).

## Usage

```bash
# audit the latest run_date in the table
python <skill_dir>/scripts/jlaw_check.py

# audit a specific date
python <skill_dir>/scripts/jlaw_check.py --date 2026-10-09

# machine-readable output
python <skill_dir>/scripts/jlaw_check.py --date 2026-10-09 --json

# fix non-standard review-type labels (the only write operation)
python <skill_dir>/scripts/jlaw_check.py --normalize-labels
```

Env: `SUPABASE_URL`, `SUPABASE_KEY` (lowercase `supabase_url` /
`supabase_service_role` accepted as fallbacks), `SUPABASE_V2_TABLE`
(default `trade-jlaw-v2`), `SUPABASE_WATCHLIST_TABLE` (default
`watchlist`). Stdlib only.

## Reading the report

- `ok: true` means no defects; the report still lists coverage counts so
  you can eyeball them.
- Every finding carries `severity` (`error` / `warn`), the affected
  symbols, and a `likely_cause` pointing at the responsible pipeline or
  runner script — use that to decide whether the fix belongs in
  `jlaw-v2-yahoo/scripts/jlaw_checklist.py`,
  `jlaw-v2-chart/scripts/chart_upsert.py`, or the reviewing agent's
  process.
- Findings on historical dates should be repaired in place (see
  `references/defect-playbook.md`); findings on today's run mean the run
  must be redone or the runner patched first.

## Bundled Resources

- `scripts/jlaw_check.py`: the audit runner (stdlib only).
- `references/defect-playbook.md`: known defect patterns seen in
  production runs, their root causes, and the exact repair SQL / steps.

## Boundaries

- Never modifies research content (scores, classifications, prices). Only
  `--normalize-labels` writes, and only the `jlaw_review_type` column.
- Never deletes rows — full-run records (Skips included) are kept by
  design; duplicates are impossible under
  `unique (run_date, symbol, jlaw_review_type)`.
- Does not re-run either pipeline; it reports, the operator decides.
