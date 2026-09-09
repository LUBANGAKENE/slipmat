-- Run this in the SQL Editor if your project's tracks table already exists
-- (schema.sql now includes these columns for anyone provisioning fresh).
--
-- Three things the scan already worked out but the library had nowhere to
-- put, so they were silently dropped on save:
--
--   artist   the track's own performer, set only when it differs from the
--            album's - a various-artists compilation, where the release is
--            credited to nobody in particular but every song has a real
--            artist. It's also what the BPM lookup searches on, so losing
--            it cost a saved compilation its tempos as well as its credits.
--
--   is_mix   Discogs' own signal that a row is one continuous mix holding
--            other songs, rather than a single recording. Worth keeping: it
--            is why that track has no tempo of its own to look up.
--
--   parts    the songs inside such a mix, each with its position ("A1a"),
--            title, duration and looked-up BPM/key.
--
-- parts is jsonb rather than child rows because those songs aren't
-- separately cueable - they have no independent existence on the record,
-- are never reordered, never land in a playlist, and are only ever read
-- back with the track holding them. A self-referencing parent row would buy
-- a join, an ordering column and its own RLS policy for nothing. Searching
-- inside it still works when you want it:
--   select * from tracks where parts @> '[{"title": "The Power"}]';
--
-- Existing rows are unaffected: artist and parts are null, is_mix false,
-- which is exactly what they were being rendered as anyway.

alter table public.tracks add column if not exists artist text;
alter table public.tracks add column if not exists is_mix boolean not null default false;
alter table public.tracks add column if not exists parts jsonb;
