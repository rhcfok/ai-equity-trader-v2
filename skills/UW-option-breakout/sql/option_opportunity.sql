-- option_opportunity.sql
-- Standard opportunity ledger for the daily Davis-playbook option workflow.
--
-- One row per published opportunity per day, idempotently upserted by
-- opportunity_key = report_date|strategy|symbol|expiry|short_strike.
-- Conventions mirror public."csp-bps-opportunity" (same lifecycle intent:
-- research ledger first, outcome tracker second).
--
-- Status lifecycle: PROPOSED -> ACTIVE -> CLOSED (outcome filled) | ROLLED |
-- ASSIGNED | SKIPPED. A day with zero qualifying setups logs one
-- NO_QUALIFIED_SETUP trace row (excluded from win-rate denominators).
--
-- Win-rate (per strategy, per period):
--   count(win = true) / count(outcome in ('WON','LOST','EXPIRED_WORTHLESS'))
-- WON / EXPIRED_WORTHLESS set win = true. ASSIGNED, ROLLED, SKIPPED and
-- NO_QUALIFIED_SETUP rows are tracked but excluded from the denominator.
--
-- Run once in the Supabase SQL editor. Idempotent.

create table if not exists public.option_opportunity (
    id                     bigint generated always as identity primary key,
    opportunity_key        text not null unique,             -- report_date|strategy|symbol|expiry|short_strike
    run_id                 text,
    report_date            date not null,
    generated_at           timestamptz not null default now(),
    status                 text not null default 'PROPOSED', -- PROPOSED|ACTIVE|CLOSED|ROLLED|ASSIGNED|SKIPPED|NO_QUALIFIED_SETUP
    outcome                text,                             -- WON|LOST|EXPIRED_WORTHLESS|EXPIRED_UNTESTED (terminal)
    win                    boolean,

    -- structure
    strategy               text not null,                    -- SHORT_PUT|BULL_PUT_SPREAD|BEAR_CALL_SPREAD
    symbol                 text not null,
    side                   text not null default 'put',      -- put|call
    expiry                 date not null,
    dte                    int,                              -- calendar days to expiry from the next trading day
    short_strike           double precision,
    short_delta            double precision,
    short_oi               double precision,
    long_strike            double precision,                 -- null for SHORT_PUT
    long_delta             double precision,
    spread_width           double precision,                 -- null for SHORT_PUT
    spot_price             double precision,
    quote_as_of            timestamptz,
    quote_status           text,

    -- signal evidence snapshot
    verdict                text,                             -- bullish|bearish|mixed
    verdict_reasons        text[] not null default '{}',
    key_flows              jsonb not null default '[]'::jsonb,
    flow_signal_date       date,
    flow_bias              text,
    bullish_z              double precision,
    bearish_z              double precision,
    net_bias_z             double precision,

    jlaw_run_date          date,
    jlaw_classification    text,
    jlaw_score             int,
    option_score           int,
    option_tier            text,
    option_alert           boolean,
    option_iv30            double precision,
    option_iv_rank         double precision,
    option_iv_trend        text,
    option_put_call_ratio  double precision,
    option_pct_below_high  double precision,

    gex_snapshot_ts        timestamptz,
    gex_put_wall           double precision,
    gex_call_wall          double precision,
    gex_net_mm             double precision,
    gex_gravity            text,
    put_wall_pct           double precision,                 -- (spot - put_wall)/spot*100 when wall < spot
    call_wall_pct          double precision,

    earnings_date          date,
    earnings_clear         boolean,                          -- earnings strictly after expiry

    -- entry economics (per share; per-contract = x100)
    short_bid              double precision,
    short_ask              double precision,
    long_bid               double precision,
    long_ask               double precision,
    entry_credit           double precision,
    collateral             double precision,                 -- CSP: strike - credit; spread: width - credit
    max_loss               double precision,
    return_on_risk         double precision,                 -- credit / max_loss
    annualized_pct         double precision,                 -- Davis filter: credit / strike / dte * 365
    davis_annualized_ok    boolean,                          -- annualized_pct >= 20
    delta_band_ok          boolean,                          -- |short delta| in [0.20, 0.35]
    score                  double precision,                 -- composite rank score from the daily run
    caution                text,
    notes                  text,

    -- 21-DTE decision point (Davis management line)
    dte21_mark_date        date,
    dte21_close_debit      double precision,
    dte21_pnl              double precision,                 -- entry_credit - dte21_close_debit

    -- terminal outcome
    exit_date              date,
    exit_quote_as_of       timestamptz,
    exit_close_debit       double precision,
    exit_pnl               double precision,                 -- entry_credit - exit_close_debit
    outcome_notes          text,

    updated_at             timestamptz not null default now()
);

create index if not exists option_opportunity_symbol_idx
    on public.option_opportunity (symbol);

create index if not exists option_opportunity_report_date_idx
    on public.option_opportunity (report_date);

create index if not exists option_opportunity_status_idx
    on public.option_opportunity (status);

create index if not exists option_opportunity_winrate_idx
    on public.option_opportunity (strategy, outcome, win);
