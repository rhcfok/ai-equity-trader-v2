---
name: trade-jlaw-v2-option-report
description: Generate ranked Unusual Whales option-breakout reports for trade-jlaw-v2 and, when explicitly requested, apply exact-row updates to its option_* fields. Use after validated option transformation results are available.
---

# Trade JLaw V2 Option Breakout Reporting

This stage is independent of the ORATS reporting pipeline. It reports and optionally updates only the enhanced-JLaw `trade-jlaw-v2` table.

## Generate a review report

```bash
python3 /home/ubuntu/skills/trade-jlaw-v2-option-report/scripts/report_trade_jlaw_v2_option_breakouts.py \
  --results /home/ubuntu/trade_jlaw_v2_option_output/YYYY-MM-DD/transform/option_transform_results.json \
  --output-dir /home/ubuntu/trade_jlaw_v2_option_output/YYYY-MM-DD/report
```

This writes a ranked Markdown report, CSV export, patch audit JSON, and report summary. It does not send email and does not change Supabase unless `--apply` is provided.

## Apply reviewed option fields

After results have been reviewed, add `--apply`. Each patch:

- matches exactly one `(id, run_date, symbol, jlaw_review_type)` row;
- writes only the 18 `option_*` columns;
- never changes JLaw methodology fields, classification, price, regime, or review type;
- fails the runner if any requested row does not update exactly once.

Verify `report_summary.json` and `supabase_option_patch_results.json` before reporting success. Do not use this skill to modify ORATS leaderboards or their schedules.
