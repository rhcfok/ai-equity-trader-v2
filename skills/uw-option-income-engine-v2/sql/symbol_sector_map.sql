-- symbol_sector_map.sql
-- One-time sector cache for the uw-option-income-2 engine.
-- Fill order: watchlist sector column (when present) -> UW /api/companies/{ticker}/profile
-- -> manual seed. Symbols left unmapped resolve to sector 'Unknown' and are exempt
-- from the per-sector cap (reported, never silently blocked).

create table if not exists public.symbol_sector_map (
    symbol      text primary key,
    sector      text not null default 'Unknown',
    subsector   text,
    source      text,                             -- watchlist|uw_profile|manual
    updated_at  timestamptz not null default now()
);

alter table public.symbol_sector_map enable row level security;

create or replace function public.uw_oi2_sector_set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists uw_oi2_sector_updated_at_trg on public.symbol_sector_map;
create trigger uw_oi2_sector_updated_at_trg
    before update on public.symbol_sector_map
    for each row execute function public.uw_oi2_sector_set_updated_at();
