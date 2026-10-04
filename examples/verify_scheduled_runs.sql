-- Run in Supabase SQL Editor after uploading the updated cli.py to GitHub.
-- Older snapshots have trigger = unknown; they cannot prove a scheduled trigger.
with runs as (
  select run_id,
         coalesce(diagnostics #>> '{execution,trigger}', 'unknown') as trigger,
         diagnostics #>> '{execution,github_run_id}' as github_run_id,
         min(started_at) as started_at,
         max(finished_at) as finished_at,
         count(*) as searches,
         sum(offer_count) as offers,
         count(*) filter (where status in ('blocked', 'error')) as failed_searches
  from public.flight_searches
  group by run_id, diagnostics #>> '{execution,trigger}',
           diagnostics #>> '{execution,github_run_id}'
)
select *,
       started_at - lag(started_at) over (order by started_at) as interval_since_previous
from runs
where trigger = 'schedule'
order by started_at desc;

-- All runs, including manual checks, and their outcomes:
select run_id, coalesce(diagnostics #>> '{execution,trigger}', 'unknown') as trigger,
       min(started_at) as started_at, count(*) as searches,
       sum(offer_count) as offers,
       count(*) filter (where status = 'blocked') as blocked_searches
from public.flight_searches
group by run_id, diagnostics #>> '{execution,trigger}'
order by started_at desc;
