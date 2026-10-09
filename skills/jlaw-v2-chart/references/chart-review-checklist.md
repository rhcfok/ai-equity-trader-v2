# J Law Chart Review Checklist (visual layer)

Read each captured chart **in this order**. Record one line per item;
anything marked ✗ feeds the verdict rules in `SKILL.md`.

## 0. Context
- [ ] Live price, day change % — note big up/down days (>±2%) and *why* if visible (news tag, earnings marker).
- [ ] Earnings flag ("E" marker) within ~2 weeks → automatic downgrade to Watch regardless of pattern (never carry a large position through earnings).

## 1. Trend / Stage (highest weight)
- [ ] Price vs 200MA: **above** (constructive) / at it / **below** (below a *falling* 200MA = automatic Skip — Stage-1 repair only).
- [ ] 200MA slope: rising / flat / falling. Flat-to-rising is acceptable; falling kills the setup.
- [ ] MA stack order: 10 > 20 > 50 and *pinched/stacked tightly* = Stage-2 ready. Wide, tangled, or inverted stack = immature.
- [ ] Distance above 50MA: >8% extended → do not chase.

## 2. Base quality
- [ ] Base shape: VCP contraction (each pullback smaller), rounding base, or loose/choppy?
- [ ] Pivot area tightness: last few weeks' range ≤10% = tight.
- [ ] Volume: dry-up into the pivot (selling exhausted) vs heavy distribution bars.
- [ ] Price position vs pivot: below pivot (waiting) / at pivot (trigger zone) / >5% above (chasing — skip).

## 3. Momentum gauges (from the Kimi layout panels)
- [ ] ADX(14): <15 no trend (coil — entry only valid at pivot with tight stop); 15–25 emerging; >25 trending. Falling ADX on a "breakout" = suspect.
- [ ] MACD(12,26,9): histogram rising through / above zero preferred; deeply negative histogram under a pivot = wait.
- [ ] RSI(14): 45–65 healthy in bases; >70 extended; <40 under a pivot = weak.

## 4. Risk geometry (merge with jlaw-v2-yahoo numbers)
- [ ] Runner stop distance ≤8%? If not, look for a **tighter technical stop** on the chart (MA shelf, base low, last higher low) and recompute RRR with it.
- [ ] With the (possibly tightened) stop: RRR ≥ 2:1 vs runner target? 
- [ ] Any obvious overhead supply (big prior top / gap down) between price and target?

## 5. Verdict inputs
Summarize as: **STRUCTURE** (Stage-2 / transition / repair), **TRIGGER** (buy-stop price or "not near"),
**STOP** (chart-validated), **ACTION** ∈ {Valid candidate, Watch, Skip}, one-line reason.
Score-flattery guard: a high runner score can never override a failing item 1 (trend/stage) — that is how NOW and NOK (score 12) were correctly gated out on 2026-10-08.
