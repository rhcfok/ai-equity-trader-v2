---
name: jlaw-v2-yahoo
description: Apply J Law's (JLawStock) trading doctrine — market regime gating, buy/sell checklists, position management rules, and the MRA review loop — to screener output and current holdings. Use when asked to run a J Law-style daily routine, evaluate breakout candidates against his buy checklist, check holdings against his sell checklist, or produce the end-of-day MRA journal.
icon: trending-up
color: Green
---

# J Law Trading Doctrine

This skill encodes the full J Law methodology learned from the member library
(Train the Trader 2.0 + Master Your Trade, jlawstock.com). It is the doctrine
layer that sits **on top of** the `jlaw-yahoo-runner` screen: the runner finds
candidates, this skill judges them the way J Law would.

> Research and analysis only. No orders, no brokerage connection, no
> personalized financial advice.

## Boundaries

- Never override the hard risk rules, even when a setup looks exciting:
  every trade needs a stop, max loss per trade ≤ average win, RRR ≥ 2:1,
  never chase >5% past the pivot, never average down, no large positions
  through earnings.
- Market regime gates everything. In **Risk-Off / 血汗錢時期** the default is
  capital preservation: raise cash, cut leverage, stand aside.
- The deterministic script evaluates only objectively computable gates.
  Pattern quality, fundamentals, and sector leadership are judgement items —
  present them as manual checklist items, never auto-pass them.

## Bundled Resources

- `scripts/jlaw_checklist.py`: deterministic gate evaluator (stdlib only).
- `references/methodology.md`: the full system (four pillars, regime filter).
- `references/checklists.md`: official pre-buy / pre-sell checklists.
- `references/rules.md`: the 38 mindset rules + 18 winner rules.
- `references/daily-routine.md`: pre-market / intraday / after-close routine.
- `references/glossary.md`: J Law's term vocabulary (VCP, PP, LVP, MRA…).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `JLAW_MIN_SCORE` | `13` | Runner score band for "Valid trade" (13–14). |
| `JLAW_MAX_CHASE_PCT` | `5` | Max % above pivot entry allowed. |
| `JLAW_MAX_STOP_PCT` | `8` | Max initial stop distance %. |
| `JLAW_MIN_RRR` | `2.0` | Min reward:risk vs entry/stop/target. |
| `JLAW_MAX_ABOVE_50MA_PCT` | `8` | Max extension above 50-day MA. |
| `JLAW_MAX_BASE_RANGE_PCT` | `15` | Max base range % (tightness proxy; J Law's pivot-area rule is ≤10%). |
| `JLAW_MAX_OFF_HIGH_PCT` | `25` | Max % below 52-week high (leaders stay near highs). |
| `JLAW_REGIME` | `Neutral` | `Risk-On` / `Neutral` / `Risk-Off` — from the weekly MYT 股市分析 or manual call. |

## Operating Workflow

### Daily routine (mirrors 每天交易部署流程)

1. **Regime first.** Set `JLAW_REGIME` from the latest weekly stance
   (MYT 股市分析 labels the market Risk-On / Neutral / Risk-Off; as of
   1 Oct 2026 it was Risk-On). If Risk-Off: report capital-preservation mode
   and do not promote new buys.
2. **Screen.** Run `jlaw-yahoo-runner` (or use its latest JSON), then:

```bash
python3 <skill_dir>/scripts/jlaw_checklist.py screen \
  --input out/jlaw_$(date -u +%F).json \
  --output out/jlaw_checklist_$(date -u +%F).json
```

3. **Holdings check.** Prepare `holdings.csv` with columns
   `symbol,avg_cost,shares,stop_loss,initial_stop` and run:

```bash
python3 <skill_dir>/scripts/jlaw_checklist.py holdings \
  --holdings holdings.csv \
  --input out/jlaw_$(date -u +%F).json
```

4. **Report in chat:** regime, A+/Valid candidates with entry/stop/target and
   RRR, sell triggers hit (stop, −8%, 1R reached → stop to breakeven,
   +15–20% → trim 20–33%), then the manual checklist items still requiring
   judgement (pattern quality, sector leadership, fundamentals, earnings
   proximity).
5. **MRA journal (after close).** Write/update the day's entry: trades taken,
   gate results, rule violations, lessons. Measure → Review → Adjust.

### Interpretation bands (from the runner, unchanged)

0–9 Skip · 10–12 Watch / Small size · 13–14 Valid trade · 15–16 Aggressive A+ setup.

## Output Contract

`screen` writes JSON: `{regime, summary, candidates:[{symbol, classification, gates:{...}, passed, failed, entry_price, stop_loss, target_price, rrr}], manual_checklist:[...]}`.
Classifications: **A+ setup** (all gates, score ≥15) · **Valid** (all gates) ·
**Watch** (score band only or ≤2 minor fails) · **Skip**.

`holdings` writes JSON: per-position `action` ∈
`STOP_HIT`, `CUT_LOSS_GT_8PCT`, `RAISE_STOP_TO_BREAKEVEN` (≥1R),
`TRIM_20_33PCT` (+15–20%), `WARNING_*`, `HOLD`.

## Failure Handling

- Missing fields for a gate → gate recorded as `unknown`, never auto-passed;
  candidate capped at **Watch**.
- Missing holdings file → skip the holdings stage with an explicit note.
- Regime unknown → treat as `Neutral` and say so.

*This is research and analysis only, not personalized financial advice.*
