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
--
-- artist is per-track and normally null: it only carries a value when it
-- differs from the album's own, which in practice means a various-artists
-- compilation where every song has a different original performer.
--
-- is_mix / parts describe a megamix or medley - one continuous groove that
-- plays several songs. parts is jsonb rather than child rows because those
-- songs are not separately cueable: they have no independent existence on
-- the record, are never reordered, never land in a playlist, and are only
-- ever read back with the track that holds them. A self-referencing parent
-- row would buy a join, an ordering column and its own RLS policy for
-- nothing. Postgres can still search inside it when needed:
--   select * from tracks where parts @> '[{"title": "The Power"}]';
create table public.tracks (
  id           uuid primary key default gen_random_uuid(),
  album_id     uuid not null references public.albums(id) on delete cascade,
  user_id      uuid not null default auth.uid() references auth.users(id) on delete cascade,
  sort_index   int not null,
  position     text,
  title        text not null,
  artist       text,
  duration     text,
  bpm          numeric,
  key_camelot  text,
  bpm_source   text,
  is_mix       boolean not null default false,
  parts        jsonb
);

create index tracks_album_id_idx on public.tracks (album_id);

alter table public.tracks enable row level security;

create policy "tracks: owner full access" on public.tracks
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());


-- Folders and playlists are nodes of one self-referencing tree, the way
-- Rekordbox itself models them - a folder is just a node other nodes can sit
-- under. parent_id null means "sits directly under the Playlists root".
--
-- This is the one place a join table is actually the right call, unlike
-- albums/tracks above: a track can sit in any number of playlists, so
-- playlist_tracks below is a genuine many-to-many, not ownership.
-- image_url holds a downscaled data: URL for a playlist's custom cover, set
-- from the detail page. Null falls back to a mosaic of the playlist's album
-- art. Stored on the row rather than in a Storage bucket for the same reason
-- the scan tool POSTs data: URLs - no infra to provision. loadPlaylists (the
-- tree data) never selects it; the browse view fetches all of them once.
create table public.playlists (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  parent_id   uuid references public.playlists(id) on delete cascade,
  kind        text not null check (kind in ('folder', 'playlist')),
  name        text not null,
  sort_index  int not null default 0,
  image_url   text,
  created_at  timestamptz not null default now()
);

create index playlists_user_id_parent_id_idx on public.playlists (user_id, parent_id);

alter table public.playlists enable row level security;

create policy "playlists: owner full access" on public.playlists
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());


-- Which tracks are in which playlist, and in what order. user_id is
-- duplicated here for the same reason as on tracks - a plain equality
-- policy instead of a join back through playlists on every row.
--
-- sort_index is bigint, not int: a drag-and-drop append writes Date.now() so
-- ordering never needs to ask "how many tracks are already in here" before
-- inserting one more. A millisecond timestamp is already ~1.8 trillion
-- today, which overflows a 4-byte int (max ~2.1 billion) - int4 was simply
-- the wrong type for the value actually being stored in it.
create table public.playlist_tracks (
  id           uuid primary key default gen_random_uuid(),
  playlist_id  uuid not null references public.playlists(id) on delete cascade,
  track_id     uuid not null references public.tracks(id) on delete cascade,
  user_id      uuid not null default auth.uid() references auth.users(id) on delete cascade,
  sort_index   bigint not null default 0,
  added_at     timestamptz not null default now()
);

create index playlist_tracks_playlist_id_idx on public.playlist_tracks (playlist_id);

alter table public.playlist_tracks enable row level security;

create policy "playlist_tracks: owner full access" on public.playlist_tracks
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());
