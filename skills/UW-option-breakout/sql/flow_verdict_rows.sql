-- flow_verdict_rows.sql
-- Normalized per-symbol companion table for "flow-verdicts".
--
-- The "flow-verdicts" table stores one whole-day verdict JSON in a single
-- payload blob per verdict_date (verdict_date PK). This table unpacks that
-- payload into one row per symbol so verdicts can be SQL-joined with
-- option_flow_symbol_signals, trade-jlaw-v2, and gex_snapshot.
--
-- Run once in the Supabase SQL editor (or via supabase db push / psql):
--   psql "$DATABASE_URL" -f flow_verdict_rows.sql
--
-- Idempotent: safe to re-run. Backfill with
--   scripts/upsert_flow_verdict_rows.py --pull-from-supabase

create table if not exists public.flow_verdict_rows (
    verdict_date        date        not null,
    symbol              text        not null,
    verdict             text        not null,           -- bullish | bearish | mixed | protective | no-data
    reasons             text[]      not null default '{}',
    caveats             text[]      not null default '{}',
    key_flows           jsonb       not null default '[]'::jsonb,
    alert_count         integer,
    earnings_block      boolean,
    next_earnings_date  date,
    classifier          text,
    tide_bias           text,                            -- from payload.market_tide.bias
    tide_date           date,                            -- from payload.market_tide.date
    tide_note           text,                            -- from payload.market_tide.note
    updated_at          timestamptz not null default now(),
    primary key (verdict_date, symbol)
);

create index if not exists flow_verdict_rows_symbol_idx
    on public.flow_verdict_rows (symbol);

create index if not exists flow_verdict_rows_verdict_idx
    on public.flow_verdict_rows (verdict_date, verdict);

-- 2026-10-29: uncapped, premium-weighted alert stats (scripts/flow_alert_stats.py).
-- alert_count stays the agent's sample size (historically capped at 20);
-- alert_count_total is every alert in the window. verdict_quant is the
-- deterministic premium-based verdict; verdict_agree = same direction class as
-- the agent verdict (bull / bear incl. protective / neutral).
alter table public.flow_verdict_rows
    add column if not exists alert_count_total  integer,
    add column if not exists alerts_capped      boolean,
    add column if not exists call_premium       numeric,
    add column if not exists put_premium        numeric,
    add column if not exists bull_premium       numeric,
    add column if not exists bear_premium       numeric,
    add column if not exists protective_premium numeric,
    add column if not exists premium_tilt       double precision,   -- (bull - bear) / (bull + bear), -1..1
    add column if not exists sweep_share        double precision,
    add column if not exists near_term_share    double precision,
    add column if not exists largest_alert      jsonb,
    add column if not exists verdict_quant      text,               -- bullish | bearish | mixed | protective | no-data
    add column if not exists verdict_agree      boolean,
    add column if not exists flow_stats_at      timestamptz;
