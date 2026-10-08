---
name: orats-leaderboard-report
description: Run Layer 2 ORATS breakout scoring, refresh conviction leaderboards, and email the daily report after the JLaw screener has finished writing n8n-breakout-jlaw rows. Use when asked for the ORATS leaderboard daily report or after JLaw completion.
icon: mail
color: Blue
related_server_ids:
- gmail
---

# ORATS Leaderboard Daily Report

Downstream only. Do not fetch charts, run vision, or write `n8n-breakout-jlaw`.

## When to run

1. Confirm secrets: `SUPABASE_URL`, `SUPABASE_KEY`, `ORATS_TOKEN`. Bind missing ones and stop. Never print secret values.
2. Confirm Gmail is connected. Add it if not.
3. Decide readiness:

```bash
python3 /home/user/skills/orats-leaderboard-report/scripts/check_jlaw_ready.py YYYY-MM-DD
```

Use UTC `YYYY-MM-DD` unless the user/scheduler gives a trading date.

| `jlaw_ready` | Action |
|---|---|
| true | Continue |
| false | JLaw not finished. If this is a scheduled wait, poll every 60s up to 90 minutes. Stop if still not ready. |

Ready means unique scored symbols ≥ 95% of `watchlist` **and** no new `n8n-breakout-jlaw` insert for 10 minutes. Failed JLaw symbols are never inserted, so do not wait for 222/222.

## Execute

```bash
python3 /home/user/skills/orats-leaderboard-report/scripts/layer2_orats_leaderboards_report.py --run-date YYYY-MM-DD --output-dir /home/user/layer2_output
```

Done only if the process exits 0.

Then send `email_payload.json` with Gmail (`body_type=html` is fine if you wrap the markdown; otherwise send the text body as plain). Recipients and subject must come from the payload. Do not change A1–A8, conviction math, recipients, or table names.

Export `email_report.txt`, `leaderboard_3.csv`, and `leaderboard_12.csv`.

## Status to return

analysis date, actionable count, ORATS success/fail, leaderboard row counts, email-send status, missing JLaw symbols, exceptions.

Continue after isolated ORATS 404/timeouts (`orats_failures.json`). Do not claim complete if the script exits non-zero or a Supabase refresh fails.
