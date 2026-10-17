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
