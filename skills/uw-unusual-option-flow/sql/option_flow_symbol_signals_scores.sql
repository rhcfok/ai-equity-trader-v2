-- option_flow_symbol_signals_scores.sql
-- Fix 4: cross-sectional flow-score columns for option_flow_symbol_signals.
--
-- Adds per-signal_date z-scores and percentile ranks so bullish/bearish flow
-- scores are comparable across symbols (raw scores are premium-scale dollars).
-- The uw-unusual-option-flow runner computes these columns at write time with
-- the same convention (see SKILL.md core data rule 7). This file backfills
-- historical rows and documents the exact SQL convention.
--
-- Run once in the Supabase SQL editor. Idempotent.

alter table public.option_flow_symbol_signals
  add column if not exists bullish_z   double precision,
  add column if not exists bearish_z   double precision,
  add column if not exists net_bias_z  double precision,
  add column if not exists bullish_pct double precision,
  add column if not exists bearish_pct double precision,
  add column if not exists net_bias_pct double precision;

-- Backfill z-scores (per signal_date cross-section)
with day_stats as (
  select signal_date,
         avg(bullish_flow_score)                         as b_mean,
         stddev(bullish_flow_score)                      as b_std,
         avg(bearish_flow_score)                         as r_mean,
         stddev(bearish_flow_score)                      as r_std,
         avg(bullish_flow_score - bearish_flow_score)    as nb_mean,
         stddev(bullish_flow_score - bearish_flow_score) as nb_std
  from public.option_flow_symbol_signals
  group by signal_date
)
update public.option_flow_symbol_signals t
set bullish_z  = round(((t.bullish_flow_score - s.b_mean) / nullif(s.b_std, 0))::numeric, 4),
    bearish_z  = round(((t.bearish_flow_score - s.r_mean) / nullif(s.r_std, 0))::numeric, 4),
    net_bias_z = round((((t.bullish_flow_score - t.bearish_flow_score) - s.nb_mean) / nullif(s.nb_std, 0))::numeric, 4)
from day_stats s
where t.signal_date = s.signal_date;

-- Backfill percentile ranks 0-100 (strict-less-than convention)
update public.option_flow_symbol_signals t
set bullish_pct   = p.bullish_pct,
    bearish_pct   = p.bearish_pct,
    net_bias_pct  = p.net_bias_pct
from (
  select id,
         round(((rank() over (partition by signal_date order by bullish_flow_score) - 1)::numeric
               / nullif(count(*) over (partition by signal_date) - 1, 0)::numeric * 100), 2) as bullish_pct,
         round(((rank() over (partition by signal_date order by bearish_flow_score) - 1)::numeric
               / nullif(count(*) over (partition by signal_date) - 1, 0)::numeric * 100), 2) as bearish_pct,
         round(((rank() over (partition by signal_date order by (bullish_flow_score - bearish_flow_score)) - 1)::numeric
               / nullif(count(*) over (partition by signal_date) - 1, 0)::numeric * 100), 2) as net_bias_pct
  from public.option_flow_symbol_signals
) p
where t.id = p.id;
