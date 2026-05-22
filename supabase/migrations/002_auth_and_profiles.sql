-- ============================================================================
-- Pricewatch: Auth + User Approval System
-- Migration 002 (v2) — fixed: no upfront super-admin seed
--
-- Change from v1: removed the upfront `insert into profiles` for the super
-- admin (it violated the FK to auth.users since no auth user existed yet).
-- Instead, the handle_new_auth_user trigger now auto-promotes any new auth
-- user whose email matches the hardcoded SUPER_ADMIN_EMAILS list.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. profiles table
-- ----------------------------------------------------------------------------
create table if not exists public.profiles (
  id              uuid primary key references auth.users(id) on delete cascade,
  email           text not null unique,
  full_name       text,
  status          text not null default 'pending'
                    check (status in ('pending','approved','rejected')),
  role            text not null default 'user'
                    check (role in ('user','super_admin')),
  requested_at    timestamptz not null default now(),
  approved_at     timestamptz,
  approved_by     uuid references public.profiles(id),
  rejected_at     timestamptz,
  rejection_reason text,
  last_sign_in_at timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists idx_profiles_status on public.profiles(status);
create index if not exists idx_profiles_email  on public.profiles(email);

-- updated_at trigger
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_profiles_updated_at on public.profiles;
create trigger trg_profiles_updated_at
  before update on public.profiles
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- 2. Pending-row reconciliation table
-- ----------------------------------------------------------------------------
-- The request-access flow needs to record an email before any auth.users row
-- exists. We can't put it in `profiles` (FK violation). So we use a small
-- staging table; when an invite is accepted and an auth.users row is created,
-- the trigger copies any matching row from access_requests into profiles
-- and deletes it from access_requests.
-- ----------------------------------------------------------------------------
create table if not exists public.access_requests (
  email        text primary key,
  full_name    text,
  requested_at timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- 3. Auto-create profile + super-admin promotion when an auth.users appears
-- ----------------------------------------------------------------------------
create or replace function public.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  super_admin_emails text[] := array['ecommerce@ukpos.com'];
  req                public.access_requests%rowtype;
  is_super           boolean;
begin
  is_super := lower(new.email) = any(super_admin_emails);

  -- See if this user had a pending access request
  select * into req from public.access_requests where lower(email) = lower(new.email);

  insert into public.profiles (id, email, full_name, status, role,
                               requested_at, approved_at)
  values (
    new.id,
    new.email,
    coalesce(req.full_name, null),
    case
      when is_super then 'approved'   -- super admin is auto-approved
      when req.email is not null then 'approved'  -- pre-approved (admin already approved them; the invite created this auth user)
      else 'pending'
    end,
    case when is_super then 'super_admin' else 'user' end,
    coalesce(req.requested_at, now()),
    case
      when is_super then now()
      when req.email is not null then now()
      else null
    end
  )
  on conflict (id) do nothing;

  -- Clean up the staging row if present
  if req.email is not null then
    delete from public.access_requests where lower(email) = lower(new.email);
  end if;

  return new;
end;
$$;

drop trigger if exists trg_on_auth_user_created on auth.users;
create trigger trg_on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_auth_user();

-- ----------------------------------------------------------------------------
-- 4. Helper: is_super_admin() and is_approved_user() for RLS policies
-- ----------------------------------------------------------------------------
create or replace function public.is_super_admin()
returns boolean
language sql
security definer
stable
set search_path = public
as $$
  select exists (
    select 1 from public.profiles
     where id = auth.uid()
       and role = 'super_admin'
       and status = 'approved'
  );
$$;

create or replace function public.is_approved_user()
returns boolean
language sql
security definer
stable
set search_path = public
as $$
  select exists (
    select 1 from public.profiles
     where id = auth.uid()
       and status = 'approved'
  );
$$;

-- ----------------------------------------------------------------------------
-- 5. RLS on profiles
-- ----------------------------------------------------------------------------
alter table public.profiles enable row level security;

drop policy if exists "users read own profile"           on public.profiles;
drop policy if exists "users update own profile minimal" on public.profiles;
drop policy if exists "super admin reads all profiles"   on public.profiles;
drop policy if exists "super admin updates profiles"     on public.profiles;
drop policy if exists "super admin deletes profiles"     on public.profiles;

create policy "users read own profile"
  on public.profiles for select
  using (id = auth.uid());

create policy "super admin reads all profiles"
  on public.profiles for select
  using (public.is_super_admin());

create policy "super admin updates profiles"
  on public.profiles for update
  using (public.is_super_admin());

create policy "super admin deletes profiles"
  on public.profiles for delete
  using (public.is_super_admin());

-- access_requests: only service role touches it (no public RLS access)
alter table public.access_requests enable row level security;
-- No policies => no access from anon/authenticated roles.
-- The backend uses the service_role key, which bypasses RLS.

-- ----------------------------------------------------------------------------
-- 6. Lock down existing data tables — only approved users can read
-- ----------------------------------------------------------------------------
drop policy if exists "Enable read access for all users" on public.skus;
drop policy if exists "Enable read access for all users" on public.competitors;
drop policy if exists "Enable read access for all users" on public.competitor_matches;
drop policy if exists "Enable read access for all users" on public.price_snapshots;
drop policy if exists "Enable read access for all users" on public.sync_runs;
drop policy if exists "Enable read access for all users" on public.alerts;

drop policy if exists "approved users read skus"               on public.skus;
drop policy if exists "approved users read competitors"        on public.competitors;
drop policy if exists "approved users read competitor_matches" on public.competitor_matches;
drop policy if exists "approved users read price_snapshots"    on public.price_snapshots;
drop policy if exists "approved users read sync_runs"          on public.sync_runs;
drop policy if exists "approved users read alerts"             on public.alerts;

create policy "approved users read skus"
  on public.skus for select using (public.is_approved_user());

create policy "approved users read competitors"
  on public.competitors for select using (public.is_approved_user());

create policy "approved users read competitor_matches"
  on public.competitor_matches for select using (public.is_approved_user());

create policy "approved users read price_snapshots"
  on public.price_snapshots for select using (public.is_approved_user());

create policy "approved users read sync_runs"
  on public.sync_runs for select using (public.is_approved_user());

create policy "approved users read alerts"
  on public.alerts for select using (public.is_approved_user());

-- ============================================================================
-- Done.
--
-- The super admin is NOT seeded here. They are auto-promoted when their
-- auth.users row is created (via the invite flow). Next steps:
--
--   1. Supabase dashboard: Authentication -> Sign In/Up -> Email
--        Allow new users to sign up = OFF
--        Confirm email = OFF
--
--   2. Supabase dashboard: Authentication -> URL Configuration
--        Site URL          = https://pricewatch-iota.vercel.app
--        Redirect URLs     = https://pricewatch-iota.vercel.app/**
--
--   3. Supabase dashboard: Authentication -> Users -> Add user -> Send invitation
--        Email: ecommerce@ukpos.com
--        Tick "Auto Confirm User"
--
--   The trigger above will spot the hardcoded ecommerce@ukpos.com address
--   and create the profile as status=approved, role=super_admin
--   automatically. They just need to click the invite email and set a
--   password.
-- ============================================================================