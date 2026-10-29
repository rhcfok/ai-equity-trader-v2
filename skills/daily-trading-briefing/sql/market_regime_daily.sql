-- public.market_regime_daily — one row per briefing date, written by
-- skills/daily-trading-briefing/scripts/regime_score.py --write-supabase.
-- Downstream (uw-options-income-engine, briefing Step 4) read band + playbook.
create table if not exists public.market_regime_daily (
  report_date      date primary key,
  as_of_session    date,
  score            numeric(5,1) not null,
  raw_band         text not null,
  band             text not null check (band in ('RISK_ON','CONSTRUCTIVE','NEUTRAL','CAUTIOUS','DEFENSIVE')),
  hysteresis       text,
  pillars          jsonb not null default '{}'::jsonb,
  missing_pillars  jsonb not null default '[]'::jsonb,
  divergences      jsonb not null default '[]'::jsonb,
  playbook         jsonb not null default '{}'::jsonb,
  score_history    jsonb not null default '[]'::jsonb,
  model_version    text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

alter table public.market_regime_daily enable row level security;

create or replace function public.market_regime_daily_touch() returns trigger
language plpgsql set search_path = public as $$
begin new.updated_at := now(); return new; end $$;

drop trigger if exists market_regime_daily_touch_trg on public.market_regime_daily;
create trigger market_regime_daily_touch_trg before update on public.market_regime_daily
  for each row execute function public.market_regime_daily_touch();
