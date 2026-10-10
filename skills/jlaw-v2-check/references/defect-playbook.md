# Defect Playbook — trade-jlaw-v2

Known defect patterns observed in production runs (first audited
2026-10-09 / 2026-10-10), their root causes, and exact repairs. Run
`scripts/jlaw_check.py` first to confirm which pattern you are seeing.

All SQL targets project `vmxxmdtzvwizrpjrrvnp`, table `trade-jlaw-v2`
(quoted — the name contains hyphens).

## 1. Wrong review-type label (`data` instead of `yahoo`)

**Symptom.** Rows with `jlaw_review_type = 'data'`; spot-check shows they
are quantitative-pipeline output (score + doctrine gates populated).

**Background.** The contract value for the quantitative pipeline
(jlaw-v2-yahoo) is `yahoo` — confirmed by the owner on 2026-10-10. A
runner wrote `data` on 2026-10-09/10; both occurrences were normalized
to `yahoo`. The only valid labels are `yahoo` (quantitative) and `chart`
(visual). **Never rewrite `yahoo` rows to `data`.**

**Repair.**

```sql
update "trade-jlaw-v2" set jlaw_review_type = 'yahoo'
where jlaw_review_type = 'data';
```

Or: `python scripts/jlaw_check.py --normalize-labels`.

**Prevention.** `JLAW_REVIEW_TYPE` defaults to `yahoo` in
`jlaw_checklist.py`; the skill contract states the only valid labels are
`yahoo` / `chart`. Keep the full record — do **not** delete rows to fix
labels.

## 2. Null cells on chart rows (jlaw_score, entry_price, …)

**Symptom.** Chart-pipeline rows with null `jlaw_score`, `entry_price`,
`target_price`, `rrr`, `vcp_stage` on reviewed (not Unreviewed) symbols.

**Root cause.** The reviewing agent's JSON did not include the extended
contract fields, or the chart was read without computing the 0–16 rubric.

**Repair.** Prefer a real re-review of the affected symbols. A heuristic
backfill from the stored `gates.reason` / `chart_score_breakdown` text is
acceptable for history (rubric: A trend, B structure, C energy, D trigger,
2 pts each; caps for below-falling-200MA / earnings < 2 wks / chasing /
no pivot).

**Prevention.** `chart_upsert.py` validates the extended contract; the
chart-review checklist §5 defines the rubric. Nulls are only acceptable on
`classification = 'Unreviewed'` rows.

## 3. Misplaced percentage / value in `entry_price`

**Symptom.** `entry_price` wildly off from `current_price` (e.g.
entry 12 on a ~$120 stock — the "12" was a stop distance %), often with a
sensible pivot quoted in `gates.rrr_note`.

**Root cause.** The reviewing agent wrote a percentage into the `pivot`
field of the review JSON; the upsert stored it verbatim (pre-guard
version of `chart_upsert.py`).

**Repair.** Recover the real pivot from `gates->>'rrr_note'` (it usually
contains "pivot X"), update the row, rescore, and flag it:

```sql
update "trade-jlaw-v2"
set entry_price = <recovered pivot>,
    gates = gates || '{"entry_price_repaired": true}'
where run_date = '<date>' and symbol = '<SYM>'
  and jlaw_review_type = 'chart';
```

**Prevention.** `chart_upsert.py` validation guard: pivot outside
`[0.5×, 2×] live_price` (or stop above `1.5× live_price`) is nulled,
flagged in `failed_gates`, and reported as a `validation_warning` — never
silently written.

## 4. `stop_pct` sign convention

**Symptom.** Negative `stop_pct` values mixed with positive ones.

**Root cause.** Two conventions: distance-below-entry written negative vs
positive distance. Chart rows additionally store distance from **current
price**, not from pivot.

**Repair / prevention.** `chart_upsert.py` normalizes with `abs()`;
`jlaw_check.py` warns on `stop_pct` outside `[0, 25]`.

## 5. Classification / score-band mismatch

**Symptom.** `classification = 'Watch'` with a score in the Skip band
(0–9), etc.

**Root cause.** Intentional hard-cap rules (earnings, below falling 200MA)
or a hand-set classification. Not automatically a defect — verify against
`gates.chart_score_breakdown` / `failed_gates` before "fixing" anything.
