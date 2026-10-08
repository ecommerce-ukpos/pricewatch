create table if not exists public.login_audit (
  id bigserial primary key,
  user_id uuid not null,
  email text not null,
  session_id text,
  logged_in_at timestamptz not null default now(),
  ip text, city text, region text, country text, user_agent text
);
create unique index if not exists login_audit_session_uq on public.login_audit(session_id) where session_id is not null;
create index if not exists login_audit_user_time on public.login_audit(user_id, logged_in_at desc);
alter table public.login_audit enable row level security;
revoke all on public.login_audit from anon, authenticated;
-- see Supabase migration 'login_audit_rpcs': record_login() and get_login_audit() SECURITY DEFINER functions
