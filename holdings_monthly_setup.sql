-- Run once in Supabase SQL Editor.
-- Render URL is pre-filled. Replace ONLY the token placeholder below.
-- Keep the real token private; do not commit it to a public GitHub repository.

create table if not exists public.fund_holdings_snapshots (
    id bigserial primary key,
    fund_name text not null,
    report_date date not null,
    source_url text not null,
    holdings jsonb not null,
    updated_at timestamptz not null default now(),
    unique (fund_name, report_date)
);

create index if not exists fund_holdings_snapshots_latest_idx
    on public.fund_holdings_snapshots (fund_name, report_date desc);

alter table public.fund_holdings_snapshots enable row level security;
grant all on public.fund_holdings_snapshots to service_role;
grant usage, select on sequence public.fund_holdings_snapshots_id_seq to service_role;

select vault.create_secret(
    'https://kenanga-investment-tool.onrender.com',
    'kenanga_render_url'
);

select vault.create_secret(
    'PASTE_THE_SAME_HOLDINGS_SYNC_TOKEN_USED_IN_RENDER',
    'kenanga_holdings_sync_token'
);

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

-- Remove an earlier job with the same name, if present, before scheduling it again.
select cron.unschedule(jobid)
from cron.job
where jobname = 'kenanga-monthly-factsheet-holdings';

select cron.schedule(
    'kenanga-monthly-factsheet-holdings',
    '15 3 5 * *',
    $$
    select net.http_post(
        url := (
            select decrypted_secret
            from vault.decrypted_secrets
            where name = 'kenanga_render_url'
            limit 1
        ) || '/api/holdings/sync',
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'X-Holdings-Token', (
                select decrypted_secret
                from vault.decrypted_secrets
                where name = 'kenanga_holdings_sync_token'
                limit 1
            )
        ),
        body := jsonb_build_object(
            'trigger', 'supabase-cron',
            'scheduled_at', now()
        ),
        timeout_milliseconds := 300000
    );
    $$
);
