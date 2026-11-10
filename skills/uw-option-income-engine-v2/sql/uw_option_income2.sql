-- uw_option_income2.sql
-- Stateless short-term credit income ledger for the uw-option-income-2 engine.
--
-- Four credit strategies only, two DTE buckets:
--   1. SHORT_PUT        (1-7 DTE "LT8" / 8-14 DTE "DTE8_14", strike <= bucket put wall)
--   2. SHORT_CALL       (stateless undefined-risk reference, strike >= bucket call wall)
--   3. BULL_PUT_SPREAD  (credit > $0.70 strict NBBO, short <= bucket put wall)
--   4. BEAR_CALL_SPREAD (credit > $0.70 strict NBBO, short >= bucket call wall)
--
-- Bucket walls are DTE-term-structure specific (computed in-engine from the UW chain,
-- gamma x OI x 100 x spot x 0.01 / 1e6 per side, nearest-expiry-per-strike inside the
-- bucket). The Supabase gex_snapshot walls (0-30 DTE blend) are stored as a fallback
-- reference only (wall_source = 'gex_blend').
--
-- Weekly plan columns carry the 10-ticker / 20-contract quota
-- (40% single-leg : 60% spreads, short call capped, <= 2 tickers per sector).
--
-- Idempotent merge pattern: PATCH by opportunity_key first, then INSERT when no row
-- matched. Terminal outcome/win fields are preserved across same-day re-runs.

create table if not exists public."uw-option-income2" (
    id                      bigint generated always as identity primary key,
    opportunity_key         text not null unique,             -- report_date|strategy|dte_bucket|symbol|short_expiry|short_strike|second_strike
    run_id                  text,
    report_date             date not null,
    generated_at            timestamptz not null default now(),
    status                  text not null default 'PROPOSED', -- PROPOSED|ACTIVE|CLOSED|ROLLED|SKIPPED|NO_QUALIFIED_SETUP
    outcome                 text,                             -- WON|LOST|EXPIRED_WORTHLESS|EXPIRED_UNTESTED|EARLY_DEFENSE
    win                     boolean,

    -- Strategy & Routing Metadata
    strategy                text not null,                    -- SHORT_PUT|SHORT_CALL|BULL_PUT_SPREAD|BEAR_CALL_SPREAD
    dte_bucket              text not null,                    -- LT8 (1-7 DTE) | DTE8_14 (8-14 DTE)
    symbol                  text not null,
    watchlist_category      text,                             -- core|satellite|watch1
    spot_price              double precision,
    quote_as_of             timestamptz,
    quote_status            text,                             -- live|close|stale

    -- Primary Short Leg
    short_option_symbol     text,                             -- OCC symbol
    short_type              text,                             -- put|call
    short_expiry            date not null,
    short_dte               int,
    short_strike            double precision,
    short_delta             double precision,
    short_iv                double precision,
    short_oi                double precision,
    short_bid               double precision,
    short_ask               double precision,

    -- Protective Long Leg (verticals only; null for single legs)
    leg2_role               text,                             -- LONG_PROTECTION|null
    leg2_option_symbol      text,
    leg2_type               text,                             -- put|call
    leg2_expiry             date,
    leg2_dte                int,
    leg2_strike             double precision,
    leg2_delta              double precision,
    leg2_iv                 double precision,
    leg2_oi                 double precision,
    leg2_bid                double precision,
    leg2_ask                double precision,

    -- Structure Economics (per share; multiply by 100 for 1 contract)
    spread_width            double precision,                 -- Strike width for verticals; null for single legs
    net_entry_price         double precision,                 -- Net credit received (all four strategies are credits)
    entry_credit            double precision,                 -- short_bid (single) or short_bid - long_ask (vertical)
    credit_per_contract_usd double precision,                 -- entry_credit * 100
    entry_debit             double precision,                 -- null (credit engine)
    collateral              double precision,                 -- BPR: strike (cash-secured put), spread width (verticals), 20% notional (short call)
    max_loss                double precision,                 -- strike - credit (put) | width - credit (vertical) | null-ish estimate (short call)
    return_on_risk          double precision,                 -- entry_credit / max_loss
    annualized_pct          double precision,
    break_even_low          double precision,
    break_even_high         double precision,
    delta_band_ok           boolean,
    credit_gate_ok          boolean,                          -- entry_credit > 0.70 for verticals; >= min single credit for legs

    -- Term-Structure Bucket Walls (primary anchors) & blended GEX reference
    bucket_put_wall         double precision,                 -- wall used for gating (bucket-specific when wall_source = 'bucket_chain')
    bucket_call_wall        double precision,
    put_wall_gex_mm         double precision,
    call_wall_gex_mm        double precision,
    wall_source             text,                             -- bucket_chain|gex_blend
    short_to_put_wall_pct   double precision,                 -- (bucket_put_wall - short_strike) / spot, put structures
    short_to_call_wall_pct  double precision,                 -- (short_strike - bucket_call_wall) / spot, call structures
    safety_floor_price      double precision,                 -- min(bucket_put_wall, jlaw_stop_loss)
    safety_ceiling_price    double precision,                 -- max(bucket_call_wall, jlaw_target_price, spot*1.03)

    -- Sector & Market Regime Context
    sector                  text,
    sector_health_rsi       double precision,                 -- mean RSI(14) of scanned sector members
    sector_health_flow      double precision,                 -- mean flow composite of scanned sector members
    market_regime           text,                             -- RISK_ON|RISK_OFF|CHOP|UNKNOWN (from trade-jlaw-v2 regime)

    -- Unusual Whales Fundamentals & Earnings
    uw_ttm_eps              double precision,
    uw_pe_ratio             double precision,
    uw_eps_growth_yoy_pct   double precision,
    uw_peg_ratio            double precision,
    uw_latest_surprise_pct  double precision,
    uw_valuation_tier       text,                             -- cheap|fair_low|fair_high|expensive|not_meaningful
    earnings_date           date,
    earnings_report_time    text,                             -- premarket|postmarket|unknown
    earnings_clear          boolean,

    -- Unusual Whales IV / Technicals / Strike-Level Flow
    uw_iv7                  double precision,
    uw_iv30                 double precision,
    uw_iv60                 double precision,
    uw_iv_rank              double precision,
    uw_iv_percentile        double precision,
    uw_term_ratio_7_30      double precision,                 -- iv7 / iv30 (front-week shock detector)
    uw_rsi_14               double precision,
    strike_adverse_sweeps   int not null default 0,           -- Ask-side sweeps (size > OI) within +-3% of short strike
    strike_whale_bids       int not null default 0,           -- Institutional bid-side writing prints

    -- Upstream Supabase Stack Signals
    verdict                 text,
    verdict_reasons         text[] not null default '{}',
    flow_signal_date        date,
    flow_alert_count        integer,
    flow_composite          double precision,
    flow_label              text,
    flow_conflicts          jsonb not null default '[]'::jsonb,
    flow_components         jsonb not null default '{}'::jsonb,

    jlaw_run_date           date,
    jlaw_classification     text,
    jlaw_score              int,
    jlaw_stop_loss          double precision,
    jlaw_target_price       double precision,

    gex_snapshot_ts         timestamptz,
    gex_regime              text,                             -- positive|negative|sub_resolution
    gex_put_wall            double precision,                 -- 0-30 DTE blend (reference)
    gex_call_wall           double precision,                 -- 0-30 DTE blend (reference)

    dp_as_of                date,
    dp_top_level            double precision,
    dp_top_level_mm         double precision,
    dp_accumulating         boolean,

    -- Composite Ranking Score (0-100) & Diagnostics
    confidence_score        double precision,
    score_band              text,                             -- STRONG (85-100) | HIGHER (70-84) | MODERATE (60-69) | LOWER (<60)
    score_components        jsonb not null default '{}'::jsonb,
    risk_flag               text,                             -- UNDEFINED_RISK (SHORT_CALL) | null
    caution                 text,
    notes                   text,

    -- Weekly Quota Plan (filled by plan_weekly_quota)
    plan_week               text,                             -- ISO week, e.g. 2026-W45
    planned_contracts       int not null default 0,           -- 1 when picked into the weekly plan, else 0
    plan_rank               int,                              -- 1..N fill order within the plan
    plan_score_adjusted     double precision,                 -- confidence + regime tilt used by the planner

    -- Automated Lifecycle Checkpoints (50% profit, mid-cycle mark, wall breach, expiry)
    profit50_hit_date       date,
    profit50_close_mark     double precision,
    midcycle_mark_date      date,                             -- first pass at <= 50% of entry DTE remaining
    midcycle_close_mark     double precision,
    midcycle_pnl            double precision,
    defense_trigger_date    date,
    defense_reason          text,                             -- BUCKET_PUT_WALL_BREACH|BUCKET_CALL_WALL_BREACH|ADVERSE_SWEEP_ALERT
    exit_date               date,
    exit_quote_as_of        timestamptz,
    exit_close_mark         double precision,
    exit_pnl                double precision,
    outcome_notes           text,

    updated_at              timestamptz not null default now()
);

create index if not exists uw_oi2_symbol_idx        on public."uw-option-income2" (symbol);
create index if not exists uw_oi2_report_date_idx   on public."uw-option-income2" (report_date);
create index if not exists uw_oi2_status_idx        on public."uw-option-income2" (status);
create index if not exists uw_oi2_plan_week_idx     on public."uw-option-income2" (plan_week);
create index if not exists uw_oi2_winrate_idx       on public."uw-option-income2" (strategy, dte_bucket, outcome, win);

-- RLS enabled; only the service-role key (used by the runner) can read/write.
alter table public."uw-option-income2" enable row level security;

-- Auto-touch updated_at on every PATCH (lifecycle pass and same-day re-runs).
create or replace function public.uw_oi2_set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists uw_oi2_updated_at_trg on public."uw-option-income2";
create trigger uw_oi2_updated_at_trg
    before update on public."uw-option-income2"
    for each row execute function public.uw_oi2_set_updated_at();
