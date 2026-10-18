# Option Trading Opportunities — 2026-10-18

Data: flow_verdict_rows 2026-10-16, option_flow_symbol_signals 2026-10-15 (normalized), trade-jlaw-v2 2026-10-16 (option layer), gex_snapshot >= 2026-10-15. Chain quotes = latest session close; re-verify before entry.

## Short puts (bullish side) — executable proposals

| symbol | spot | score | earn | expiry (DTE) | 30Δ contract | credit | Δ | ann% | 20Δ contract | credit | Δ | ann% |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| CRM | 239.56 | 12.5 | 2026-12-02 | 2026-11-20 (32) | CRM261120P00230000 | 7.22 | 0.33 | 36% | CRM261120P00220000 | 4.38 | 0.22 | 23% |
| SNOW | 304.53 | 12.5 | 2026-12-02 | 2026-11-20 (32) | SNOW261120P00280000 | 9.78 | 0.27 | 40% | SNOW261120P00270000 | 7.10 | 0.21 | 30% |
| BLK | 1131.50 | 11.5 | 2027-01-21 | 2026-11-20 (32) | BLK261120P01090000 | 19.30 | 0.30 | 20% | BLK261120P01050000 | 10.90 | 0.19 | 12% |
| PENG | 56.08 | 11.0 | 2027-01-05 | 2026-11-20 (32) | PENG261120P00050000 | 3.00 | 0.27 | 68% | PENG261120P00045000 | 1.55 | 0.16 | 39% |
| SMTC | 134.00 | 11.0 | 2026-11-23 | 2026-11-20 (32) | SMTC261120P00125000 | 7.15 | 0.31 | 65% | SMTC261120P00115000 | 3.75 | 0.19 | 37% |
| ADI | 395.40 | 10.5 | 2026-11-24 | 2026-11-20 (32) | ADI261120P00370000 | 9.10 | 0.28 | 28% | ADI261120P00360000 | 6.55 | 0.21 | 21% |
| ASML | 1640.41 | 9.5 | 2027-01-27 | 2026-11-20 (32) | ASML261120P01550000 | 40.90 | 0.30 | 30% | ASML261120P01490000 | 26.80 | 0.21 | 21% |
| AVGO | 330.30 | 9.0 | 2026-12-10 | 2026-11-20 (32) | AVGO261120P00310000 | 7.85 | 0.29 | 29% | AVGO261120P00300000 | 5.28 | 0.21 | 20% |
| HPE | 54.41 | 9.0 | 2026-12-03 | 2026-11-20 (32) | HPE261120P00050000 | 2.28 | 0.28 | 52% | HPE261120P00047000 | 1.46 | 0.19 | 35% |
| CRWD | 226.40 | 8.5 | 2026-12-01 | 2026-11-20 (32) | CRWD261120P00210000 | 7.80 | 0.29 | 42% | CRWD261120P00197500 | 4.08 | 0.19 | 24% |
| QQQ | 707.87 | 8.5 | — | 2026-11-20 (32) | QQQ261120P00684000 | 10.91 | 0.30 | 18% | QQQ261120P00660000 | 6.87 | 0.19 | 12% |
| MRVL | 226.90 | 8.5 | 2026-12-01 | 2026-11-20 (32) | MRVL261120P00210000 | 9.72 | 0.30 | 53% | MRVL261120P00195000 | 5.45 | 0.19 | 32% |
| CRDO | 169.29 | 7.5 | 2026-12-07 | 2026-11-20 (32) | CRDO261120P00155000 | 9.75 | 0.31 | 72% | CRDO261120P00140000 | 5.05 | 0.19 | 41% |
| DELL | 449.00 | 7.5 | 2026-11-24 | 2026-11-20 (32) | DELL261120P00410000 | 18.30 | 0.28 | 51% | DELL261120P00390000 | 12.50 | 0.21 | 37% |

## Bull put spreads (5-wide) — executable proposals

| symbol | spot | expiry (DTE) | short contract | long contract | net credit | max loss | ROC |
|---|---|---|---|---|---|---|---|
| PENG | 56.08 | 2026-11-20 (32) | PENG261120P00050000 | PENG261120P00045000 | 1.45 | 3.55 | 41% |
| SMTC | 134.00 | 2026-11-20 (32) | SMTC261120P00125000 | SMTC261120P00120000 | 1.85 | 3.15 | 59% |
| HPE | 54.41 | 2026-11-20 (32) | HPE261120P00050000 | HPE261120P00045000 | 1.32 | 3.68 | 36% |
| CRWD | 226.40 | 2026-11-20 (32) | CRWD261120P00210000 | CRWD261120P00205000 | 1.58 | 3.42 | 46% |
| QQQ | 707.87 | 2026-11-20 (32) | QQQ261120P00684000 | QQQ261120P00679000 | 1.02 | 3.98 | 26% |
| CRDO | 169.29 | 2026-11-20 (32) | CRDO261120P00155000 | CRDO261120P00150000 | 1.80 | 3.20 | 56% |

## Post-earnings watchlist (strong data, earnings inside the window — sell puts AFTER the print)

| symbol | verdict | score | earn | why |
|---|---|---|---|---|
| XOM | bullish | 13.0 | 2026-10-30 | alert=True, tier=MODERATE, ivR=64, flow=insufficient_evidence |
| NVDA | bullish | 13.0 | 2026-11-18 | alert=True, tier=WEAK, ivR=28, flow=bullish |
| PLTR | mixed | 12.5 | 2026-11-02 | alert=True, tier=WEAK, ivR=60, flow=bullish |
| AAPL | mixed | 11.5 | 2026-11-02 | alert=True, tier=DEVELOPING, ivR=55, flow=bullish |
| SHOP | bullish | 11.5 | 2026-11-03 | alert=False, tier=WEAK, ivR=77, flow=bullish |
| EBAY | bullish | 11.5 | 2026-11-04 | alert=True, tier=MODERATE, ivR=52, flow=- |
| AMD | bullish | 11.5 | 2026-11-03 | alert=True, tier=WEAK, ivR=35, flow=mixed |
| ETN | bullish | 11.5 | 2026-11-03 | alert=True, tier=DEVELOPING, ivR=72, flow=bullish |
| DD | bullish | 11.0 | 2026-11-03 | alert=True, tier=MODERATE, ivR=48, flow=- |
| DUOL | mixed | 11.0 | 2026-11-03 | alert=True, tier=MODERATE, ivR=42, flow=- |

## Bear call spreads — executable proposals

| symbol | spot | expiry (DTE) | 30Δ contract | credit | Δ | 20Δ contract | credit | Δ |
|---|---|---|---|---|---|---|---|---|
| SMH | 541.50 | 2026-11-20 (32) | SMH261120C00575000 | 11.80 | 0.31 | SMH261120C00595000 | 7.05 | 0.21 |
| SOXX | 493.67 | 2026-11-20 (32) | SOXX261120C00530000 | 11.15 | 0.31 | SOXX261120C00555000 | 7.40 | 0.20 |
| KEYS | 330.03 | 2026-11-20 (32) | KEYS261120C00360000 | 8.95 | 0.31 | KEYS261120C00380000 | 4.25 | 0.19 |
| UNH | 400.80 | 2026-11-20 (32) | UNH261120C00420000 | 8.22 | 0.34 | UNH261120C00440000 | 3.48 | 0.18 |
| KLAC | 166.20 | 2026-11-20 (32) | KLAC261120C00186000 | 5.55 | 0.30 | KLAC261120C00196000 | 3.20 | 0.20 |
| NET | 294.50 | 2026-11-20 (32) | NET261120C00340000 | 12.88 | 0.32 | NET261120C00370000 | 7.20 | 0.20 |

Davis guardrails: entry on a red/oversold day only; strike below support (GEX put wall); 20-30 delta;
DTE <= 45; annualized premium >= 20% filter; risk 5-7% of capital per trade via BPR; exit/decide at 21 DTE.

Ledger: skipped (--no-log).
