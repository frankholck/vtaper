-- V-Taper Coach online save: one private record, reachable only through passcode-checked functions.
-- Run once in the Supabase project "vtaper-coach". The app calls vtaper_load / vtaper_save.
create extension if not exists pgcrypto with schema extensions;

create schema if not exists private;
revoke all on schema private from public, anon, authenticated;

create table private.vtaper_store (
  id smallint primary key default 1 check (id = 1),
  secret_hash text,
  data jsonb,
  rev bigint not null default 0,
  updated_at timestamptz,
  failed_attempts int not null default 0,
  locked_until timestamptz
);
insert into private.vtaper_store (id) values (1);
alter table private.vtaper_store enable row level security;

-- Last saved state of each day, kept 60 days, as a safety net.
create table private.vtaper_snapshots (
  day date primary key,
  data jsonb not null,
  rev bigint not null,
  saved_at timestamptz not null default now()
);
alter table private.vtaper_snapshots enable row level security;

-- Returns: ok | created | locked | short | not_set_up | bad_passcode
create or replace function private.vtaper_check(p_pass text, p_allow_setup boolean)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  s private.vtaper_store%rowtype;
begin
  select * into s from private.vtaper_store where id = 1 for update;
  if s.locked_until is not null and s.locked_until > now() then
    return 'locked';
  end if;
  if p_pass is null or length(p_pass) < 8 or length(p_pass) > 200 then
    return 'short';
  end if;
  if s.secret_hash is null then
    if not p_allow_setup then
      return 'not_set_up';
    end if;
    update private.vtaper_store
       set secret_hash = extensions.crypt(p_pass, extensions.gen_salt('bf', 10)),
           failed_attempts = 0, locked_until = null
     where id = 1;
    return 'created';
  end if;
  if extensions.crypt(p_pass, s.secret_hash) = s.secret_hash then
    if s.failed_attempts <> 0 or s.locked_until is not null then
      update private.vtaper_store set failed_attempts = 0, locked_until = null where id = 1;
    end if;
    return 'ok';
  end if;
  -- five wrong passcodes in a row lock the store for 15 minutes
  update private.vtaper_store
     set failed_attempts = case when failed_attempts + 1 >= 5 then 0 else failed_attempts + 1 end,
         locked_until    = case when failed_attempts + 1 >= 5 then now() + interval '15 minutes' else null end
   where id = 1;
  return 'bad_passcode';
end;
$$;

create or replace function public.vtaper_load(p_pass text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  r text;
  s private.vtaper_store%rowtype;
begin
  r := private.vtaper_check(p_pass, true);
  if r not in ('ok', 'created') then
    return jsonb_build_object('ok', false, 'error', r);
  end if;
  select * into s from private.vtaper_store where id = 1;
  return jsonb_build_object('ok', true, 'created', r = 'created', 'rev', s.rev, 'data', s.data, 'updated_at', s.updated_at);
end;
$$;

create or replace function public.vtaper_save(p_pass text, p_data jsonb, p_base_rev bigint)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  r text;
  s private.vtaper_store%rowtype;
  new_rev bigint;
  new_at timestamptz;
begin
  r := private.vtaper_check(p_pass, false);
  if r <> 'ok' then
    return jsonb_build_object('ok', false, 'error', r);
  end if;
  if p_data is null or jsonb_typeof(p_data) <> 'object' or octet_length(p_data::text) > 3000000 then
    return jsonb_build_object('ok', false, 'error', 'bad_data');
  end if;
  select * into s from private.vtaper_store where id = 1 for update;
  if s.rev <> coalesce(p_base_rev, -1) then
    return jsonb_build_object('ok', false, 'error', 'conflict', 'rev', s.rev, 'data', s.data);
  end if;
  update private.vtaper_store
     set data = p_data, rev = rev + 1, updated_at = now()
   where id = 1
   returning rev, updated_at into new_rev, new_at;
  insert into private.vtaper_snapshots (day, data, rev)
  values ((now() at time zone 'Asia/Dubai')::date, p_data, new_rev)
  on conflict (day) do update set data = excluded.data, rev = excluded.rev, saved_at = now();
  delete from private.vtaper_snapshots where day < (now() at time zone 'Asia/Dubai')::date - 60;
  return jsonb_build_object('ok', true, 'rev', new_rev, 'updated_at', new_at);
end;
$$;

-- Lightweight call used to keep the free-tier project awake.
create or replace function public.vtaper_ping()
returns text
language sql
security definer
set search_path = ''
as $$ select 'ok'::text from private.vtaper_store where id = 1 $$;

revoke all on function private.vtaper_check(text, boolean) from public, anon, authenticated;
revoke all on function public.vtaper_load(text) from public;
revoke all on function public.vtaper_save(text, jsonb, bigint) from public;
revoke all on function public.vtaper_ping() from public;
grant execute on function public.vtaper_load(text) to anon, authenticated;
grant execute on function public.vtaper_save(text, jsonb, bigint) to anon, authenticated;
grant execute on function public.vtaper_ping() to anon, authenticated;
