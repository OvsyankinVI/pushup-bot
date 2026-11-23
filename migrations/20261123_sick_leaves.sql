-- Sick leave v1: all dates are inclusive, Europe/Moscow calendar days.
create extension if not exists btree_gist;
create table if not exists public.sick_leave_doctors (
 chat_id bigint not null,
 telegram_user_id bigint not null,
 appointed_by bigint not null,
 created_at timestamptz not null default now(),
 primary key(chat_id,telegram_user_id),
 foreign key(chat_id,telegram_user_id) references public.members(chat_id,telegram_user_id) on delete cascade
);
create table if not exists public.sick_leaves (
 id bigint generated always as identity primary key,
 chat_id bigint not null,
 patient_id bigint not null,
 starts_on date not null,
 ends_on date not null,
 created_by bigint not null,
 updated_by bigint,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 deleted_at timestamptz,
 deleted_by bigint,
 constraint sick_leave_valid_dates check (ends_on >= starts_on),
 foreign key(chat_id,patient_id) references public.members(chat_id,telegram_user_id),
 foreign key(chat_id,created_by) references public.members(chat_id,telegram_user_id),
 constraint sick_leave_no_overlap exclude using gist (
  chat_id with =, patient_id with =,
  daterange(starts_on,ends_on,'[]') with &&
 ) where (deleted_at is null)
);
create index if not exists sick_leaves_active_idx on public.sick_leaves(chat_id,starts_on,ends_on) where deleted_at is null;
create table if not exists public.sick_leave_events (
 id bigint generated always as identity primary key,
 sick_leave_id bigint not null references public.sick_leaves(id),
 chat_id bigint not null,
 actor_id bigint not null,
 event_type text not null check(event_type in ('created','updated','deleted','recovered')),
 before_state jsonb,
 after_state jsonb,
 created_at timestamptz not null default now(),
 delivered_at timestamptz,
 delivery_attempts integer not null default 0
);
create unique index if not exists sick_leave_recovered_once on public.sick_leave_events(sick_leave_id) where event_type='recovered';
create index if not exists sick_leave_events_pending on public.sick_leave_events(created_at) where delivered_at is null;
alter table public.sick_leave_doctors enable row level security;
alter table public.sick_leaves enable row level security;
alter table public.sick_leave_events enable row level security;
-- No anon/authenticated policies: only server-side service role may access.
revoke all on public.sick_leave_doctors, public.sick_leaves, public.sick_leave_events from anon, authenticated;
revoke all on sequence public.sick_leaves_id_seq, public.sick_leave_events_id_seq from anon, authenticated;