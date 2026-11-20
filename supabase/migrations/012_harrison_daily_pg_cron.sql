-- 012: Start the Harrison morning stock check from Supabase, not GitHub's schedule.
-- GitHub scheduled runs were starting 6–7 hours late, so Supabase now decides when it starts:
--   pg_cron runs harrison_daily_tick() every minute 04:00–08:59 UTC (covers 05:00–08:30 UK
--   in both GMT and BST). The first tick of each UK day picks a random start time between
--   05:00 and 08:30 UK and saves it in harrison_daily_plan. At that time the tick calls the
--   smart-function Edge Function {mode:"harrison_daily"}, which starts
--   .github/workflows/harrison_daily.yml (trigger=daily). The scraper skips the run if a daily
--   run already started today, so a repeat call is harmless.
-- The key used is the public anon key the web app already uses; the Edge Function holds the
-- GitHub token. Applied 20 Nov 2026.

create extension if not exists pg_cron;
create extension if not exists pg_net;

create table if not exists public.harrison_daily_plan (
  day           date primary key,                 -- UK date
  target_at     timestamptz not null,             -- random time 05:00–08:30 UK
  dispatched_at timestamptz,
  request_id    bigint,                           -- pg_net request id (see net._http_response)
  created_at    timestamptz not null default now()
);
alter table public.harrison_daily_plan enable row level security;
drop policy if exists "read daily plan" on public.harrison_daily_plan;
create policy "read daily plan" on public.harrison_daily_plan for select to authenticated using (true);

create or replace function public.harrison_daily_tick() returns void
language plpgsql security definer set search_path = public, extensions as $$
declare
  today date := (now() at time zone 'Europe/London')::date;
  p public.harrison_daily_plan;
  rid bigint;
begin
  insert into public.harrison_daily_plan (day, target_at)
  values (today, ((today + time '05:00') + random() * interval '210 minutes') at time zone 'Europe/London')
  on conflict (day) do nothing;

  select * into p from public.harrison_daily_plan where day = today for update;
  if p.dispatched_at is not null or now() < p.target_at then
    return;
  end if;
  if now() > p.target_at + interval '2 hours' then
    return;                                         -- missed by a lot (e.g. database paused): skip today
  end if;

  select net.http_post(
    url     := 'https://uaqakssusydpjzrcznhb.supabase.co/functions/v1/smart-function',
    body    := jsonb_build_object('mode', 'harrison_daily'),
    headers := jsonb_build_object('Content-Type', 'application/json',
                                  'Authorization', 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVhcWFrc3N1c3lkcGp6cmN6bmhiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg3NDM3ODUsImV4cCI6MjA5NDMxOTc4NX0.OM0XBJsFb6hlLKXLoNpahuH4zzGcnNR3W-bzKfKPZ-w')
  ) into rid;

  update public.harrison_daily_plan set dispatched_at = now(), request_id = rid where day = today;
end $$;

revoke all on function public.harrison_daily_tick() from public, anon, authenticated;

select cron.unschedule('harrison-daily-tick') where exists (select 1 from cron.job where jobname = 'harrison-daily-tick');
select cron.schedule('harrison-daily-tick', '* 4-8 * * *', 'select public.harrison_daily_tick()');
