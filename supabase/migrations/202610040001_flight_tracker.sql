-- Run once in Supabase SQL Editor. Independent from the team's theatre tables.
begin;

create table public.flight_searches (
  id uuid primary key,
  run_id uuid not null,
  event_id text not null,
  origin text not null check (origin ~ '^[A-Z]{3}$'),
  departure_date date not null,
  event_start timestamptz not null,
  arrival_buffer_minutes integer not null check (arrival_buffer_minutes >= 0),
  started_at timestamptz not null,
  finished_at timestamptz not null,
  observed_at timestamptz not null,
  status text not null check (status in ('ok','no_matching_offers','error','blocked')),
  error text,
  search_url text not null,
  offer_count integer not null check (offer_count >= 0),
  eligible_count integer not null check (eligible_count >= 0),
  diagnostics jsonb not null
);

create table public.flight_observations (
  search_id uuid not null references public.flight_searches(id),
  offer_key text not null,
  flight_key text not null,
  fare_key text not null,
  origin text not null,
  destination text not null check (destination in ('FCO','CIA')),
  departure_at timestamptz not null,
  arrival_at timestamptz not null check (arrival_at > departure_at),
  marketing_carrier text not null,
  operating_carrier text,
  flight_number text not null,
  duration_minutes integer not null check (duration_minutes > 0),
  aircraft text,
  cabin text not null check (cabin = 'ECONOMY'),
  fare_name text,
  price_amount numeric(12,2) not null check (price_amount >= 0),
  currency text not null check (currency = 'EUR'),
  passengers integer not null check (passengers = 1),
  available boolean not null,
  seats_available integer check (seats_available >= 0),
  seats_source text not null,
  cabin_baggage jsonb,
  checked_baggage jsonb,
  arrival_deadline timestamptz not null,
  eligible_for_event boolean not null,
  minutes_until_event integer not null,
  search_url text not null,
  raw jsonb not null,
  primary key (search_id, offer_key)
);

create index flight_searches_route_time on public.flight_searches(event_id, origin, departure_date, observed_at desc);
create index flight_observations_fare on public.flight_observations(fare_key, search_id);

alter table public.flight_searches enable row level security;
alter table public.flight_observations enable row level security;
revoke all on public.flight_searches, public.flight_observations from anon, authenticated;
grant select, insert on public.flight_searches, public.flight_observations to service_role;

-- One transaction per route/date. A retry after a lost response cannot duplicate observations.
create function public.ingest_flight_snapshot(p_snapshot jsonb)
returns uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_search public.flight_searches;
  v_offer jsonb;
  v_inserted uuid;
begin
  if (p_snapshot->>'schema_version')::integer is distinct from 1
     or jsonb_typeof(p_snapshot->'offers') is distinct from 'array' then
    raise exception 'Invalid snapshot envelope';
  end if;
  select * into v_search from jsonb_populate_record(null::public.flight_searches, p_snapshot->'search');
  if v_search.offer_count <> jsonb_array_length(p_snapshot->'offers') then
    raise exception 'Offer count mismatch';
  end if;
  if v_search.status in ('error','blocked','no_matching_offers') and v_search.offer_count <> 0 then
    raise exception 'Unsuccessful search cannot contain offers';
  end if;
  insert into public.flight_searches select (v_search).* on conflict (id) do nothing
    returning id into v_inserted;
  if v_inserted is null then
    return v_search.id;
  end if;
  for v_offer in select value from jsonb_array_elements(p_snapshot->'offers') loop
    insert into public.flight_observations
    select * from jsonb_populate_record(null::public.flight_observations,
      v_offer || jsonb_build_object('search_id',v_search.id));
  end loop;
  return v_search.id;
end;
$$;
revoke all on function public.ingest_flight_snapshot(jsonb) from public, anon, authenticated;
grant execute on function public.ingest_flight_snapshot(jsonb) to service_role;

-- Latest attempt per route/date; old offers are never presented as current after an error.
create view public.latest_event_flights with (security_invoker = true) as
with latest as (
  select distinct on (event_id, origin, departure_date) *
  from public.flight_searches
  order by event_id, origin, departure_date, observed_at desc, id desc
)
select s.event_id, s.observed_at, s.status as search_status, o.*
from latest s join public.flight_observations o on o.search_id = s.id
where s.status = 'ok' and o.available and o.eligible_for_event;

-- Minimum comparable quote per fare per snapshot; unknown fare names limit comparability.
create view public.flight_price_history with (security_invoker = true) as
with prices as (
 select s.event_id, s.id as search_id, s.observed_at, o.fare_key,
        min(o.price_amount) as price_amount
 from public.flight_searches s join public.flight_observations o on o.search_id = s.id
 where s.status = 'ok' and o.available
 group by s.event_id, s.id, s.observed_at, o.fare_key
)
select *, price_amount - lag(price_amount) over (
  partition by event_id, fare_key order by observed_at, search_id
) as change_eur from prices;

revoke all on public.latest_event_flights, public.flight_price_history from anon, authenticated;
grant select on public.latest_event_flights, public.flight_price_history to service_role;
commit;
