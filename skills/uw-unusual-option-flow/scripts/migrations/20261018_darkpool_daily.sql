-- Dark pool accumulation workflow v2 — daily aggregate table.
-- Run via scripts/apply_migrations.py or the Supabase SQL editor. Idempotent.
--
-- One row per (symbol, fetch_date) with the reduced accumulation summary so
-- the daily opportunity generation step can JOIN it at publish time without
-- depending on raw prints or on run ordering.

create table if not exists darkpool_symbol_daily (
  symbol text not null,
  fetch_date date not null,
  window_start timestamptz,
  window_end timestamptz,
  print_count integer,
  total_premium numeric,
  buy_premium numeric,
  sell_premium numeric,
  net_premium numeric,
  block_count integer,
  block_premium numeric,
  top_level numeric,
  top_level_premium numeric,
  above_close_premium numeric,
  below_close_premium numeric,
  accumulating boolean,
  updated_at timestamptz default now(),
  primary key (symbol, fetch_date)
);

create index if not exists darkpool_symbol_daily_date_idx
  on darkpool_symbol_daily (fetch_date desc, symbol);
