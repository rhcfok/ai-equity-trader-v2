-- Dark pool accumulation workflow v1 — run once in the Supabase SQL editor
-- (Database > SQL editor). Idempotent; safe to re-run.
--
-- Creates the raw print table written by scripts/darkpool_fetch.py and adds
-- the dp_* aggregate columns to the existing Davis opportunity table.

create table if not exists darkpool_prints (
  symbol text not null,
  executed_at timestamptz not null,
  tracking_id bigint not null,
  size integer,
  price numeric,
  premium numeric,
  nbbo_bid numeric,
  nbbo_ask numeric,
  market_center text,
  sale_cond_codes text,
  trade_code text,
  inserted_at timestamptz default now(),
  primary key (symbol, executed_at, tracking_id)
);

create index if not exists darkpool_prints_symbol_time_idx
  on darkpool_prints (symbol, executed_at desc);

alter table "option-opportunity-davis"
  add column if not exists dp_as_of timestamptz,
  add column if not exists dp_total_premium_mm numeric,
  add column if not exists dp_buy_mm numeric,
  add column if not exists dp_sell_mm numeric,
  add column if not exists dp_net_mm numeric,
  add column if not exists dp_blocks integer,
  add column if not exists dp_top_level numeric,
  add column if not exists dp_top_level_mm numeric,
  add column if not exists dp_accumulating boolean;
