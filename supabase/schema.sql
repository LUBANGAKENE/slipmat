-- Slipmat library.
--
-- Run once in the Supabase SQL editor for your project (or via
-- `supabase db push` if you use the CLI). See README.md "Library" for the
-- two env vars this pairs with.
--
-- auth.users is provided by Supabase the moment Auth is enabled - there is
-- nothing to create for it. user_id below references it directly.
--
-- Each scanned album is a personal artifact, not a shared catalogue entry -
-- the whole point of Slipmat is that the sleeve in your hands is ground
-- truth for *your* pressing, not a canonical release. Two people scanning
-- the same 1979 pressing get two independent rows. That's why user_id is a
-- plain foreign key on albums rather than a join table: ownership here is
-- one-to-many, not many-to-many.

create table public.albums (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null default auth.uid() references auth.users(id) on delete cascade,
  artist            text,
  album             text,
  year              text,
  label             text,
  catalog_number    text,
  confidence        text,
  notes             text,
  tracklist_source  text,
  model             text,
  cover_url         text,
  created_at        timestamptz not null default now()
);

create index albums_user_id_created_at_idx on public.albums (user_id, created_at desc);

alter table public.albums enable row level security;

create policy "albums: owner full access" on public.albums
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());


-- One row per track on a saved album. user_id is duplicated from albums
-- rather than checked via a join, so the RLS policy below is a plain
-- equality check instead of a subquery on every row.
--
-- sort_index preserves printed order explicitly rather than relying on
-- `position` to sort correctly - "A10" would sort before "A2" as text.
create table public.tracks (
  id           uuid primary key default gen_random_uuid(),
  album_id     uuid not null references public.albums(id) on delete cascade,
  user_id      uuid not null default auth.uid() references auth.users(id) on delete cascade,
  sort_index   int not null,
  position     text,
  title        text not null,
  duration     text,
  bpm          numeric,
  key_camelot  text,
  bpm_source   text
);

create index tracks_album_id_idx on public.tracks (album_id);

alter table public.tracks enable row level security;

create policy "tracks: owner full access" on public.tracks
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());
