-- EM Expected-Move model tables (idempotent; run via Supabase SQL editor or
-- the Management API /database/query endpoint). RLS enabled with no policies:
-- only the service-role key can read/write, matching the repo's other tables.

create table if not exists public."em-model-snapshot" (
  symbol text not null,
  as_of date not null,
  close numeric,
  sigma_daily numeric,
  calib_cov_30d numeric,
  calib_n integer,
  model_version text not null default 'gaussian-drift-off-v1',
  updated_at timestamptz not null default now(),
  primary key (symbol, as_of)
);
alter table public."em-model-snapshot" enable row level security;

create table if not exists public."em-probability" (
  source_table text not null,
  opportunity_key text not null,
  report_date date not null,
  symbol text,
  strategy text,
  bucket text,
  spot numeric,
  strike numeric,
  option_type text,
  expiry date,
  h_trading_days integer,
  credit numeric,
  delta numeric,
  p_win_expiry numeric,
  p_profit50_first numeric,
  outside_band112 boolean,
  calib_cov_30d numeric,
  earnings_in_window boolean,
  model_version text,
  sigma_as_of date,
  scored_at timestamptz not null default now(),
  primary key (source_table, opportunity_key)
);
alter table public."em-probability" enable row level security;
