-- Run this in the SQL Editor if your project already has the playlists table
-- from 0003 (schema.sql now includes this column for anyone provisioning fresh).
--
-- A playlist can carry its own cover image now - the detail page lets you pick
-- one. It's stored as a downscaled data: URL right on the row, the same shape
-- the scan tool already POSTs its photos in: no Storage bucket to provision,
-- and a ~600px JPEG at q0.82 is 40-90 KB, fine for the handful of playlists
-- anyone actually makes. Null means "no custom cover" - the page falls back to
-- a 2x2 mosaic of the playlist's album art, then to a plain note glyph.
--
-- loadPlaylists() (the tree data) deliberately does NOT select this column.
-- The browse grid/list pulls every playlist's cover once per session via
-- loadBrowseCovers(); the detail page fetches just the open playlist's.

alter table public.playlists add column image_url text;
