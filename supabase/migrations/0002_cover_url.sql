-- Run this in the SQL Editor if your project's albums table already exists
-- (schema.sql now includes this column for anyone provisioning fresh).
--
-- No image is stored anywhere - this holds a URL to art already hosted by
-- the lookup source (iTunes' CDN), resolved once client-side on first view
-- and cached here so the grid doesn't re-fetch it every time.

alter table public.albums add column if not exists cover_url text;
