-- option_flow_symbol_signals_scores.sql
-- Fix 4: cross-sectional flow-score columns for option_flow_symbol_signals.
--
-- Adds per-signal_date robust (median/MAD) z-scores and percentile ranks so bullish/bearish flow
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

-- Backfill robust z-scores (per signal_date cross-section).
-- x = sign(score) * ln(1 + |score| / 1e6)   (signed log of $ premium; heavy tails)
-- z = (x - median) / (1.4826 * MAD), clipped to +/-5. Falls back to stddev when
-- MAD = 0 and to NULL with < 3 rows or zero dispersion. Replaces the plain
-- mean/stddev z (2026-10-29): one outlier (MU +$448M on 10-28, z 6.66) pushed
-- ~45/61 names to about -0.3 and broke every downstream z threshold.
with base as (
  select id, signal_date,
         sign(bullish_flow_score) * ln(1 + abs(bullish_flow_score) / 1e6) as b,
         sign(bearish_flow_score) * ln(1 + abs(bearish_flow_score) / 1e6) as r,
         sign(coalesce(bullish_flow_score, 0) - coalesce(bearish_flow_score, 0))
           * ln(1 + abs(coalesce(bullish_flow_score, 0) - coalesce(bearish_flow_score, 0)) / 1e6) as nb
  from public.option_flow_symbol_signals
),
med as (
  select signal_date, count(*) as n,
         percentile_cont(0.5) within group (order by b)  as b_med,
         percentile_cont(0.5) within group (order by r)  as r_med,
         percentile_cont(0.5) within group (order by nb) as nb_med,
         stddev(b) as b_std, stddev(r) as r_std, stddev(nb) as nb_std
  from base group by signal_date
),
mad as (
  select m.signal_date, m.n, m.b_med, m.r_med, m.nb_med,
         coalesce(nullif(1.4826 * percentile_cont(0.5) within group (order by abs(x.b  - m.b_med)), 0),  m.b_std)  as b_s,
         coalesce(nullif(1.4826 * percentile_cont(0.5) within group (order by abs(x.r  - m.r_med)), 0),  m.r_std)  as r_s,
         coalesce(nullif(1.4826 * percentile_cont(0.5) within group (order by abs(x.nb - m.nb_med)), 0), m.nb_std) as nb_s
  from med m join base x using (signal_date)
  group by m.signal_date, m.n, m.b_med, m.r_med, m.nb_med, m.b_std, m.r_std, m.nb_std
)
update public.option_flow_symbol_signals t
set bullish_z  = case when s.n < 3 then null else round(greatest(-5, least(5, (x.b  - s.b_med)  / nullif(s.b_s, 0)))::numeric, 4) end,
    bearish_z  = case when s.n < 3 then null else round(greatest(-5, least(5, (x.r  - s.r_med)  / nullif(s.r_s, 0)))::numeric, 4) end,
    net_bias_z = case when s.n < 3 then null else round(greatest(-5, least(5, (x.nb - s.nb_med) / nullif(s.nb_s, 0)))::numeric, 4) end
from base x join mad s using (signal_date)
where t.id = x.id;

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
