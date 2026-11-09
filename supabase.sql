-- svg-bench public reviews — Supabase/Postgres schema.
--
-- Run this in the Supabase SQL editor, then put the project URL and *anon* key
-- into svg-bench/site/config.js (or inject them at build time).
--
-- The anon key is public. These RLS policies allow anyone to read non-revoked
-- reviews and insert new reviews, and allow nobody to update or delete them
-- through the anon role. Moderate with the service_role key in the dashboard.

create extension if not exists pgcrypto;

create table if not exists public.svgbench_reviews (
  id          uuid primary key default gen_random_uuid(),
  run_id      text        not null,
  model       text        not null default '',
  task        text        not null default '',
  stars       smallint    not null check (stars between 1 and 5),
  note        text        not null default '' check (char_length(note) <= 2000),
  reviewer    text        not null default 'anonymous' check (char_length(reviewer) <= 60),
  revoked     boolean     not null default false,
  created_at  timestamptz not null default now()
);

create index if not exists svgbench_reviews_run_id_idx
  on public.svgbench_reviews (run_id, created_at desc);

create index if not exists svgbench_reviews_created_at_idx
  on public.svgbench_reviews (created_at desc);

alter table public.svgbench_reviews enable row level security;

-- Public read: only visible reviews.
drop policy if exists "svgbench public read" on public.svgbench_reviews;
create policy "svgbench public read"
  on public.svgbench_reviews for select
  using (revoked = false);

-- Public write: insert only, never revoked, never a privileged field.
drop policy if exists "svgbench public insert" on public.svgbench_reviews;
create policy "svgbench public insert"
  on public.svgbench_reviews for insert
  with check (revoked = false);

grant select, insert on public.svgbench_reviews to anon;
grant select, insert on public.svgbench_reviews to authenticated;

-- Optional: a view with the aggregate used by the name cell.
create or replace view public.svgbench_review_stats as
select run_id,
       count(*)::int as review_count,
       round(avg(stars)::numeric, 2) as avg_stars
from public.svgbench_reviews
where revoked = false
group by run_id;

grant select on public.svgbench_review_stats to anon;
grant select on public.svgbench_review_stats to authenticated;
