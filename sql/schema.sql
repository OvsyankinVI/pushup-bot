-- Run manually in Supabase SQL Editor. Backend uses a secret/service_role key.
begin;
create table if not exists public.members (
    id bigint generated always as identity primary key,
    telegram_user_id bigint not null,
    chat_id bigint not null,
    username text,
    display_name text not null,
    active boolean not null default true,
    created_at timestamptz not null default now(),
    unique (chat_id, telegram_user_id)
);
create table if not exists public.daily_reports (
    id bigint generated always as identity primary key,
    telegram_user_id bigint not null,
    chat_id bigint not null,
    report_date date not null,
    created_at timestamptz not null default now(),
    unique (chat_id, telegram_user_id, report_date),
    foreign key (chat_id, telegram_user_id)
        references public.members (chat_id, telegram_user_id)
);
create index if not exists daily_reports_chat_date_idx
    on public.daily_reports (chat_id, report_date);
-- No public API access; backend service role bypasses RLS.
alter table public.members enable row level security;
alter table public.daily_reports enable row level security;
revoke all on public.members, public.daily_reports from anon, authenticated;
grant all on public.members, public.daily_reports to service_role;
grant usage, select on sequence public.members_id_seq, public.daily_reports_id_seq to service_role;
commit;
