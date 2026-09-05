-- Run this in the SQL Editor if your project already has albums/tracks
-- (schema.sql now includes these two tables for anyone provisioning fresh).
--
-- Folders and playlists are nodes of one self-referencing tree - a folder is
-- just a node other nodes can sit under. parent_id null means "sits directly
-- under the Playlists root".

create table public.playlists (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  parent_id   uuid references public.playlists(id) on delete cascade,
  kind        text not null check (kind in ('folder', 'playlist')),
  name        text not null,
  sort_index  int not null default 0,
  created_at  timestamptz not null default now()
);

create index playlists_user_id_parent_id_idx on public.playlists (user_id, parent_id);

alter table public.playlists enable row level security;

create policy "playlists: owner full access" on public.playlists
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());


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
