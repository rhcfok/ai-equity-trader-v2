-- quant2 (technical-direction-probability) result tables
-- Run this once in the Supabase SQL editor, then the upsert script fills them.
-- Study: 18 predeclared daily technical conditions vs 7-session up-close,
-- strict walk-forward + untouched holdout + 8 gates. Honest negatives included.

create table if not exists quant2_runs (
  id                   bigint generated always as identity primary key,
  run_name             text not null unique,
  run_ts               timestamptz not null default now(),
  config_sha256        text,
  registry_sha256      text,
  eval_first           date,
  eval_last            date,
  dev_start            date,
  dev_end              date,
  holdout_start        date,
  holdout_end          date,
  n_tickers            integer,
  n_obs                integer,
  history_years        numeric,
  validated_candidates text[],
  probability_status   text,
  calibration_pass     boolean,
  validation_overall   text,
  checks               jsonb,
  created_at           timestamptz not null default now()
);

create table if not exists quant2_rule_scorecard (
  id               bigint generated always as identity primary key,
  run_name         text not null references quant2_runs(run_name) on delete cascade,
  rule             text not null,
  definition       text,
  is_combo         boolean,
  -- walk-forward (development window) selection metrics
  wf_n             numeric,
  wf_dir_lift      numeric,
  wf_ret_lift      numeric,
  wf_worst5        numeric,
  -- untouched holdout metrics
  ho_raw_days      integer,
  ho_n             integer,
  ho_tickers       integer,
  ho_win_rate      numeric,
  ho_wilson_lo     numeric,
  ho_wilson_hi     numeric,
  ho_mean_ret      numeric,
  ho_median_ret    numeric,
  ho_mean_mae      numeric,
  ho_mean_mfe      numeric,
  ho_worst5        numeric,
  dir_lift         numeric,
  ret_lift         numeric,
  tk_dir_delta     numeric,
  tk_dir_ci_lo     numeric,
  tk_dir_ci_hi     numeric,
  tk_ret_delta     numeric,
  tk_ret_ci_lo     numeric,
  tk_ret_ci_hi     numeric,
  mc_dir_diff      numeric,
  mc_dir_ci_lo     numeric,
  mc_dir_ci_hi     numeric,
  p_raw            numeric,
  q_bh             numeric,
  p_holm           numeric,
  sub_dir_lifts    text,
  loo_ticker_min   numeric,
  loo_quarter_min  numeric,
  loo_month_min    numeric,
  -- eight holdout gates
  g1_episodes      boolean,
  g2_lift          boolean,
  g3_ticker_adj    boolean,
  g4_bootstrap_ci  boolean,
  g5_subperiods    boolean,
  g6_worst5        boolean,
  g7_fdr           boolean,
  g8_concentration boolean,
  passes_all       boolean,
  created_at       timestamptz not null default now(),
  unique (run_name, rule)
);

create table if not exists quant2_leaderboard (
  id                    bigint generated always as identity primary key,
  run_name              text not null references quant2_runs(run_name) on delete cascade,
  rank                  integer,
  ticker                text not null,
  grp                   text,          -- predeclared group (sector/asset class)
  as_of                 date,
  score_hist_freq       numeric,       -- historical frequency, NOT a calibrated probability
  n_active_rules        integer,
  active_rules          text,
  base_wr_holdout       numeric,       -- ticker own-baseline holdout win rate
  n_obs                 integer,
  rsi14                 numeric,
  f_rsi_oversold        boolean,
  f_rsi_deep_weakness   boolean,
  f_rsi_slope3_up       boolean,
  f_rsi_slope5_up       boolean,
  f_rsi_slope7_up       boolean,
  f_rsi_regslope7_up    boolean,
  f_rsi_repair3         boolean,
  f_rsi_lowzone_repair  boolean,
  f_adx_bullish         boolean,
  f_trend_alignment     boolean,
  f_sector_rs           boolean,
  f_benchmark_not_weak  boolean,
  f_volume_breakout     boolean,
  f_volume_confirm      boolean,
  f_obv_breakout        boolean,
  f_bb_lower_rebound    boolean,
  f_macd_improve        boolean,
  f_sma20_reclaim       boolean,
  created_at            timestamptz not null default now(),
  unique (run_name, ticker)
);

create table if not exists quant2_mae_strike_bridge (
  id           bigint generated always as identity primary key,
  run_name     text not null references quant2_runs(run_name) on delete cascade,
  sample       text not null,
  n            integer,
  touch_2pct   numeric,   -- P(low dips >=2% below next-open entry within 7 sessions)
  touch_3pct   numeric,
  touch_5pct   numeric,
  touch_7pct   numeric,
  touch_10pct  numeric,
  touch_13pct  numeric,
  mae_median   numeric,
  created_at   timestamptz not null default now(),
  unique (run_name, sample)
);
