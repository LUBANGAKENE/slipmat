-- Run this if you already ran 0003_playlists.sql before this fix - it
-- shipped playlist_tracks.sort_index as a 4-byte int (max ~2.1 billion).
-- A drag-and-drop append writes Date.now(), a millisecond timestamp already
-- around 1.8 trillion today, which overflows that immediately: "value ...
-- is out of range for type integer". Existing rows (there won't be many,
-- since this is exactly what was failing to insert) carry over unchanged -
-- bigint is a strict widening, not a type change that loses anything.

alter table public.playlist_tracks alter column sort_index type bigint;
