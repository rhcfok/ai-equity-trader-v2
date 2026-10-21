-- uw_option_opportunities.sql
-- Unified stateless opportunity ledger for the uw-options-income-engine workflow.
--
-- Supports all 5 stateless strategy families:
--   1. WHEEL_CSP        (Core ~32 DTE 20-28d put & Tactical 5-10 DTE <20d put)
--   2. WHEEL_CC         (Stateless covered-call reference board at/above GEX Call Wall)
--   3. BULL_PUT_SPREAD  (5-wide if spot < $150, 10-wide if spot >= $150, |delta| <= 0.28)
--   4. SHORT_STRANGLE   (30-45 DTE 14-20d put below Put Wall + call above Call Wall in positive GEX)
--   5. DIAGONAL_SPREAD  (60-90 DTE 70-80d ITM long call + 7-21 DTE 20-30d OTM short call in low IV)
--   6. BEAR_CALL_SPREAD (Bearish flow + DP distribution 5/10-wide call credit spread)
--
-- Stateless by design: never reads portfolio holdings or brokerage positions.
-- Every published PROPOSED row is tracked hypothetically from publication through
-- the 50%-profit auto-win checkpoint, the 21-DTE Davis decision mark, and expiry.
--
-- Idempotent merge pattern: PATCH by opportunity_key first, then INSERT when no row matched.
-- Terminal outcome/win fields are preserved across same-day re-runs.

create table if not exists public."uw-option-opportunities" (
    id                      bigint generated always as identity primary key,
    opportunity_key         text not null unique,             -- report_date|strategy|bucket|symbol|short_expiry|short_strike|second_strike
    run_id                  text,
    report_date             date not null,
    generated_at            timestamptz not null default now(),
    status                  text not null default 'PROPOSED', -- PROPOSED|ACTIVE|CLOSED|ROLLED|SKIPPED|NO_QUALIFIED_SETUP
    outcome                 text,                             -- WON|LOST|EXPIRED_WORTHLESS|EXPIRED_UNTESTED|EARLY_DEFENSE
    win                     boolean,

    -- Strategy & Routing Metadata
    strategy                text not null,                    -- WHEEL_CSP|WHEEL_CC|BULL_PUT_SPREAD|SHORT_STRANGLE|DIAGONAL_SPREAD|BEAR_CALL_SPREAD
    dte_bucket              text not null default 'CORE',     -- CORE (~32d) | TACTICAL (5-10d) | STRANGLE (30-45d) | DIAGONAL (60-90d/7-21d)
    symbol                  text not null,
    watchlist_category      text,
    spot_price              double precision,
    quote_as_of             timestamptz,
    quote_status            text,                             -- live|close|stale

    -- Primary Short Leg (Put for WHEEL_CSP / BULL_PUT_SPREAD / SHORT_STRANGLE; Call for WHEEL_CC / DIAGONAL_SPREAD / BEAR_CALL_SPREAD)
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

    -- Secondary Leg:
    --   - For BULL_PUT_SPREAD / BEAR_CALL_SPREAD: protective long leg (same expiry)
    --   - For SHORT_STRANGLE: short call leg (same expiry, leg2_side = 'SHORT_CALL')
    --   - For DIAGONAL_SPREAD: back-month ITM long call leg (60-90 DTE, leg2_side = 'LONG_BACK_CALL')
    leg2_role               text,                             -- LONG_PROTECTION|SHORT_STRANGLE_CALL|LONG_DIAGONAL_BACK|null
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
    spread_width            double precision,                 -- Strike width for verticals/diagonals; corridor width for strangles
    net_entry_price         double precision,                 -- Positive = net credit received; Negative = net debit paid (DIAGONAL_SPREAD)
    entry_credit            double precision,                 -- Net credit for credit strategies (null for debit diagonal)
    entry_debit             double precision,                 -- Net debit for DIAGONAL_SPREAD (null for credit strategies)
    collateral              double precision,                 -- BPR / collateral required per share
    max_loss                double precision,
    return_on_risk          double precision,                 -- entry_credit / max_loss (or max_diagonal_cycle_yield)
    annualized_pct          double precision,                 -- credit / strike / dte * 365 * 100
    break_even_low          double precision,
    break_even_high         double precision,
    davis_annualized_ok     boolean,
    delta_band_ok           boolean,
    safety_floor_price      double precision,                 -- min(jlaw_stop, gex_put_wall, dp_top_level)
    safety_ceiling_price    double precision,                 -- max(jlaw_target, gex_call_wall)

    -- Unusual Whales Fundamental & Earnings Replacement (Replaces equity_valuation_signal_daily)
    uw_ttm_eps              double precision,
    uw_pe_ratio             double precision,
    uw_eps_growth_yoy_pct   double precision,
    uw_peg_ratio            double precision,
    uw_latest_surprise_pct  double precision,
    uw_analyst_target       double precision,
    uw_analyst_recent_action text,                            -- upgraded|target raised|maintained|downgraded|target lowered
    uw_valuation_tier       text,                             -- cheap|fair_low|fair_high|expensive|not_meaningful
    earnings_date           date,
    earnings_report_time    text,                             -- premarket|postmarket|unknown
    earnings_clear          boolean,

    -- Unusual Whales IV, Technical & Strike-Level Flow Signals
    uw_iv7                  double precision,
    uw_iv30                 double precision,
    uw_iv60                 double precision,
    uw_iv90                 double precision,
    uw_iv_rank              double precision,
    uw_iv_percentile        double precision,
    uw_term_ratio_7_30      double precision,                 -- iv7 / iv30 (backwardation shock detector)
    uw_term_ratio_30_60     double precision,                 -- iv30 / iv60
    uw_rsi_14               double precision,                 -- Daily RSI(14) from /api/stock/{ticker}/technical-indicator/RSI
    uw_stance_fit_score     double precision,                 -- 0-5 from /api/stock/{ticker}/option-stance
    strike_adverse_sweeps   int not null default 0,           -- Ask-side put/call sweeps (size > OI) within +-3% of short strike
    strike_whale_put_bids   int not null default 0,           -- Institutional bid-side put writing prints confirming floor

    -- Upstream Supabase Stack Signals (JLaw + Flow Verdicts + GEX + Dark Pool)
    verdict                 text,
    verdict_reasons         text[] not null default '{}',
    key_flows               jsonb not null default '[]'::jsonb,
    flow_signal_date        date,
    flow_bias               text,
    bullish_z               double precision,
    bearish_z               double precision,
    net_bias_z              double precision,

    jlaw_run_date           date,
    jlaw_classification     text,
    jlaw_score              int,
    jlaw_stop_loss          double precision,
    jlaw_target_price       double precision,
    option_score            int,
    option_tier             text,
    option_alert            boolean,

    gex_snapshot_ts         timestamptz,
    gex_regime              text,                             -- positive|negative|sub_resolution
    gex_put_wall            double precision,
    gex_call_wall           double precision,
    gex_net_mm              double precision,
    gex_gravity             text,
    put_wall_pct            double precision,
    call_wall_pct           double precision,

    dp_as_of                date,
    dp_total_premium_mm     double precision,
    dp_buy_mm               double precision,
    dp_sell_mm              double precision,
    dp_net_mm               double precision,
    dp_blocks               int,
    dp_top_level            double precision,
    dp_top_level_mm         double precision,
    dp_accumulating         boolean,

    -- Composite Ranking Score (0-100) & Diagnostics
    confidence_score        double precision,
    score_band              text,                             -- STRONG (85-100) | HIGHER (70-84) | MODERATE (60-69) | LOWER (<60)
    score_components        jsonb not null default '{}'::jsonb,
    caution                 text,
    notes                   text,

    -- Automated Hypothetical Lifecycle Checkpoints (50% Profit, 21-DTE Davis Line, GEX Wall Breach, Expiry)
    profit50_hit_date       date,
    profit50_close_mark     double precision,
    dte21_mark_date         date,
    dte21_close_mark        double precision,
    dte21_pnl               double precision,
    defense_trigger_date    date,
    defense_reason          text,                             -- GEX_PUT_WALL_BREACH|GEX_CALL_WALL_BREACH|ADVERSE_SWEEP_ALERT
    exit_date               date,
    exit_quote_as_of        timestamptz,
    exit_close_mark         double precision,
    exit_pnl                double precision,
    outcome_notes           text,

    updated_at              timestamptz not null default now()
);

create index if not exists uw_opt_opp_symbol_idx
    on public."uw-option-opportunities" (symbol);

create index if not exists uw_opt_opp_report_date_idx
    on public."uw-option-opportunities" (report_date);

create index if not exists uw_opt_opp_status_idx
    on public."uw-option-opportunities" (status);

create index if not exists uw_opt_opp_winrate_idx
    on public."uw-option-opportunities" (strategy, dte_bucket, outcome, win);

-- RLS enabled (service-role key writes; no anon/authenticated policies).
-- NOTE: already applied to Supabase project vmxxmdtzvwizrpjrrvnp via migration
-- `create_uw_option_opportunities` (2026-10-21).
alter table public."uw-option-opportunities" enable row level security;

-- Auto-touch updated_at on every PATCH (lifecycle pass and same-day re-runs).
create or replace function public.uw_opt_opp_set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists uw_opt_opp_updated_at_trg on public."uw-option-opportunities";
create trigger uw_opt_opp_updated_at_trg
    before update on public."uw-option-opportunities"
    for each row execute function public.uw_opt_opp_set_updated_at();
