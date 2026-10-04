-- Apply AFTER migration 001. Does not delete or rewrite existing observations.
-- MB budgets use decimal bytes. Keep 100 MB below the plan limit for other growth.
begin;

-- Fixed read-only metadata query; elevated execution permits cluster-wide size accounting.
create or replace function public.flight_storage_usage()
returns jsonb
language sql
security definer
set search_path = ''
as $$
  select pg_catalog.jsonb_build_object(
    'database_bytes', (select sum(pg_catalog.pg_database_size(d.oid)) from pg_catalog.pg_database d),
    'flight_bytes', pg_catalog.pg_total_relation_size('public.flight_searches'::regclass)
                  + pg_catalog.pg_total_relation_size('public.flight_observations'::regclass),
    'flight_budget_bytes', 250000000,
    'database_stop_bytes', 400000000
  );
$$;
revoke all on function public.flight_storage_usage() from public, anon, authenticated;
grant execute on function public.flight_storage_usage() to service_role;

create or replace function public.ingest_flight_snapshot(p_snapshot jsonb)
returns uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_search public.flight_searches;
  v_offer jsonb;
  v_inserted uuid;
  v_usage jsonb;
  v_headroom bigint;
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
  -- Serialize this scraper's writers; theatre writes remain independently controlled.
  perform pg_catalog.pg_advisory_xact_lock(20261004, 210500);
  if exists (select 1 from public.flight_searches where id = v_search.id) then
    return v_search.id;
  end if;
  select public.flight_storage_usage() into v_usage;
  -- Allow for row/index/page growth. This is a conservative estimate, not a storage reservation.
  v_headroom := 2000000 + 4 * pg_catalog.octet_length(p_snapshot::text);
  if (v_usage->>'database_bytes')::bigint + v_headroom >= 400000000
     or (v_usage->>'flight_bytes')::bigint + v_headroom >= 250000000 then
    return null;
  end if;
  insert into public.flight_searches select (v_search).* on conflict (id) do nothing
    returning id into v_inserted;
  if v_inserted is null then
    return v_search.id;
  end if;
  for v_offer in select value from jsonb_array_elements(p_snapshot->'offers') loop
    insert into public.flight_observations
    select * from jsonb_populate_record(null::public.flight_observations,
      v_offer || jsonb_build_object('search_id',v_search.id, 'raw', '{}'::jsonb));
  end loop;
  return v_search.id;
end;
$$;
revoke all on function public.ingest_flight_snapshot(jsonb) from public, anon, authenticated;
grant execute on function public.ingest_flight_snapshot(jsonb) to service_role;

commit;
