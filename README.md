# Slipmat

Photograph a vinyl record, get back the artist, album, and tracklist — with the
side positions (`A1`, `B3`) that tell you where to drop the needle.

Built for DJs coming to vinyl from digital. Rekordbox tells you the BPM and key
because it analysed the audio file. With vinyl there is no file, so Slipmat
starts from the object in your hands: it reads the text printed on the sleeve.

![status](https://img.shields.io/badge/status-work%20in%20progress-orange)

---

## What it does today

- Drag in a photo of the front sleeve, back cover, or centre label
- A vision model reads the printed text and returns structured data
- Falls back to Discogs, then MusicBrainz, when the photo shows no tracklist
  — or, when the sleeve names no album, identifies the release from the
  tracklist instead
- Prefers vinyl pressings over CD releases, so you get `A1/B2` rather than `1,2,3`
- Runs on your phone over local wifi — camera or gallery, whichever you need
- Fills in BPM and Camelot key per track where a catalogue knows them
- Optional accounts and a saved library, if you point it at a Supabase project

## BPM and key

The results table fills its BPM/key column in a second pass, once the tracklist
is on screen. [`bpm.py`](bpm.py) resolves each track through a permanent cache
and then ReccoBeats — which still serves the audio-features schema Spotify
retired — and [`analyze.py`](analyze.py) turns that into Camelot notation,
half/double-time candidates and pitch-fader percentages.

```bash
python bpm.py "Mr. Fingers" "Mystery of Love"
python bpm.py --record scan.json      # annotate a whole tracklist
python bpm.py --check                 # are the Spotify credentials working?
```

**Add the Spotify credentials.** Without them the lookup falls back to
ReccoBeats' own search, which takes a title with no artist term — so it misses
on common titles, and common titles are exactly what hit records have. Scanning
*Off the Wall* without the hop loses *Rock With You*, *Girlfriend* and *I Can't
Help It*, not because they are obscure but because their titles are. See
[Setup](#spotify-credentials-optional-for-bpm-and-key).

Expect blanks on underground 12"s. We start from a photograph, so there is no
audio to measure — every number is looked up, and streaming catalogues never
carried those records. A blank column is the honest answer there.

---

## Setup

Requires Python 3.10+.

```bash
git clone https://github.com/LUBANGAKENE/slipmat.git
cd slipmat
pip install -r requirements.txt

cp .env.example .env      # then put your key in it
python app.py
```

Open <http://127.0.0.1:5000/app>. `/` is the homepage; the scan tool itself
lives at `/app`. The server binds on all interfaces and prints a LAN address,
so you can open it on your phone on the same wifi — tapping the drop zone
there offers the camera or your photo gallery, whichever has the shot, which
is how you would actually use this next to the decks.

### API key

Vision runs through [OpenRouter](https://openrouter.ai). Put your key in `.env`:

```
OPENROUTER_API_KEY=sk-or-v1-...
VINYL_MODEL=google/gemini-2.5-flash
```

Roughly $0.001 per scan with Flash — about 1,000 records per dollar. Any
vision-capable OpenRouter model works; change `VINYL_MODEL` to swap.

### Discogs token (optional, better identification)

Add one line to `.env`:

```
DISCOGS_TOKEN=...
```

Get it free at <https://www.discogs.com/settings/developers> — log in,
**Generate new token**. No app review, no redirect URI, nothing else to fill
in.

This is what `identify.py` reaches for first whenever the sleeve shows no
tracklist, or shows a tracklist but no album name — Discogs is built around
individual vinyl pressings and catalogue numbers in a way MusicBrainz isn't,
so it recognises records MusicBrainz misses. Leave it unset and Slipmat falls
back to MusicBrainz alone, exactly as before.

### Spotify credentials (optional, for BPM and key)

Add two more lines to the same `.env`:

```
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
```

Get them free at <https://developer.spotify.com/dashboard> — log in, **Create
app**, any name and description, any redirect URI (`http://localhost:5000` is
fine), tick the Web API box. The client ID and secret are on the app's settings
page. No review, no user login, no cost.

Nothing here plays audio or touches your account: Spotify is used purely as a
search engine, to turn "artist + title read off a sleeve" into a track ID that
ReccoBeats can answer. Then check it:

```bash
python bpm.py --check
```

That reports whether the credentials are present and accepted, then resolves a
track that only lands when the hop is working. Restart `app.py` afterwards —
`.env` is read at startup.

### Library (optional, sign in and save)

1. Create a free project at <https://supabase.com>.
2. Open its **SQL Editor** and run [`supabase/schema.sql`](supabase/schema.sql) —
   creates `albums` and `tracks`, with Row Level Security so each account only
   ever sees its own.
3. From **Project Settings → API**, add to `.env`:
   ```
   SUPABASE_URL=...
   SUPABASE_ANON_KEY=...
   ```
   The anon key is meant to be public — it goes to the browser, not just this
   process. What actually protects the data is the Row Level Security in
   `schema.sql`, not keeping this secret. Leave both blank to run exactly as
   before, with no accounts and no save button.
4. Restart `app.py`.

Sign-in is email and password, handled entirely by Supabase Auth — this repo
never sees a password. Once signed in, the header shows a circular initial
avatar in place of the sign-in form — click it for the **Profile** page
(email, Sign out); it isn't one of the Scan/Library tabs, so neither shows
active while it's open, and clicking either tab is what takes you back out.
A **Save to library** button appears under a scanned record; a **Library**
tab lists everything saved, with a delete on each entry. The browser talks to
Supabase directly for all of this — Flask never touches it, so `/api/scan`
and `/api/bpm` are unchanged either way.

The Library tab is Spotify's shape on top of Rekordbox's data. **Albums** and
**Artists** browse cover-art grids — Artists is grouped from the same rows,
most recently active first; clicking one filters Albums down to theirs, with
a chip to clear it. **Tracks** is Spotify's Liked Songs: artwork, title over
artist, and a row of playlist bubbles across the top — tap one to filter down
to that playlist, tap it again to clear, or tap the dashed **+** at the end
to create a new one right there without leaving the page. BPM and Camelot
key stay on every row, because those two numbers are the reason this app
exists; the layout adapts around them rather than copying the reference
exactly, and a **Sort** bar underneath lets you order the list by either —
click again to reverse (key sorts around the Camelot wheel, not
alphabetically), and the choice is shared with **All Tracks** under
Playlists, so picking one here carries over there. The `⋯` on a row opens a
bottom sheet with **Add to playlist**, which is the touch-reachable path to
what drag-and-drop does with a mouse — you can also drag a row straight onto
a bubble. Clicking any album tile drops into the full Rekordbox-style detail
instead: every track with its BPM and Camelot key, grouped back under its
album, same as the scan results table.

**Playlists** opens as a browsable wall of covers — a **grid** or a **list**,
toggled top-right. **All Tracks** sits first, styled like Spotify's Liked
Songs; then your playlists, each showing its own photo or, until you set one,
a 2×2 mosaic of its album art. The **+** in the header makes a new one.
Clicking **All Tracks** opens every saved track as artwork rows, sortable by
BPM or Key — that sort is the one shared with the Tracks page. Clicking a
playlist opens its page: tap the cover to replace it with a photo (downscaled
in the browser and stored on the row — [`0005`](supabase/migrations/0005_playlist_image.sql)),
**Add** opens a searchable picker of every library track not already in it
(BPM and key on each), and **Sort** chips reorder the view — **Added** is the
default and keeps the order you built it in; BPM and Key re-sort what's shown
without ever rewriting that stored order. A track's `⋯` here offers **Remove
from this playlist**. Unlike Albums/Artists/Tracks — all just different views
over the albums you've saved — playlists are real rows: a track can sit in
any number of them, which is the one place in this schema a join table is
actually the right call (`playlists`, a self-referencing tree of folders and
playlists, and `playlist_tracks`, which track sits in which playlist and in
what order).

Album covers come from the iTunes Search API (free, no key, CORS-open)
matched on artist + album, resolved once and cached back onto the row. Artist
photos come from Deezer's search API instead — iTunes has no artist images at
all — resolved via JSONP (Deezer sends no CORS header, so a plain `fetch()`
is blocked; a `<script>` tag is the standard workaround) and cached in
`localStorage`, since there's no artist row in the database to cache it on —
Artists is computed client-side from the albums already fetched, not its own
table. Both degrade the same way: no match found leaves the plain tile rather
than a wrong photo.

**Already ran `schema.sql` before?** Run whichever of these you're missing, in
order, in the SQL Editor — each is a no-op if you're already caught up:
- [`supabase/migrations/0002_cover_url.sql`](supabase/migrations/0002_cover_url.sql)
  — `cover_url` on `albums`. Without it the grid still works, covers just
  re-fetch every visit instead of caching, and the console says why.
- [`supabase/migrations/0003_playlists.sql`](supabase/migrations/0003_playlists.sql)
  — the `playlists` and `playlist_tracks` tables. Without it the Playlists
  pill shows a permission/relation error instead of the cover wall.
- [`supabase/migrations/0004_playlist_tracks_bigint.sql`](supabase/migrations/0004_playlist_tracks_bigint.sql)
  — only if you ran `0003` before this fix. `sort_index` shipped as a 4-byte
  `int`, and adding a track appends `Date.now()`, a millisecond timestamp that
  overflows it immediately — the insert fails with `value ... is out of range
  for type integer`. New `0003` runs already get the right type; this widens
  an existing one.
- [`supabase/migrations/0005_playlist_image.sql`](supabase/migrations/0005_playlist_image.sql)
  — `image_url` on `playlists`, for a playlist's custom cover. Without it the
  detail page still works, it just can't save the photo you pick (and the
  console says why); the album-art mosaic is unaffected.

---

## Deploying to Vercel

`vercel.json` builds `app.py` with `@vercel/python` and routes every path to
it, so the Flask app runs unchanged and sees the real URL. Import the repo in
Vercel, add the same environment variables you put in `.env` (Project →
Settings → Environment Variables), and deploy — `requirements.txt` and the
templates are picked up automatically.

One caveat worth knowing before you rely on it: a scan is a single vision-model
call that regularly runs **15–40 seconds**, and Vercel caps a function at 10 s
on the Hobby plan — `/api/scan` and a large `/api/bpm` batch will time out
there. Everything else is fine on any plan: the homepage, the scan UI itself,
and the entire signed-in library (albums, tracks, playlists) talk straight to
Supabase from the browser and never touch the function timeout. If scanning
has to work reliably on a free tier, a host that runs a persistent process —
Render, Railway, Fly.io — fits this app better than serverless.

---

## How it works

**1. Read, don't recall.** The prompt tells the model to transcribe what is
printed and return `null` for anything illegible. Without that instruction it
pattern-matches a familiar cover and recites a tracklist from training data —
which looks correct and is unverifiable.

**2. Sleeve first, database second.** The record in your hands is ground truth.
A database lookup can only guess *which pressing you own*, and the 1979 12"
has different timings from the 2016 reissue. Discogs (falling back to
MusicBrainz) is consulted for the tracklist only when the photos show none at
all. It fills in the other direction too: when the tracklist *is* legible but
the sleeve never names the release — a white label, a cover that's all
artwork — the model is asked which record those songs are from, and the same
database pass confirms the answer before it's written. A hits compilation is
the exception: a dozen of them carry the same singles, so it can't be pinned
from its tracklist — it's just labelled *Various Artists* and left there. The
tracklist itself is always the sleeve's; only the name is looked up.

**3. The recogniser proposes, the database disposes.** When a fallback runs,
the model's output is a hypothesis, never an answer. Discogs is tried first —
its catalogue of individual vinyl pressings runs deeper than MusicBrainz's for
the records this app scans — and MusicBrainz is tried only when Discogs isn't
configured, is down, or comes back with nothing better than a weak match.
Either way, results are searched strictly, then fuzzily (so a misread `Rumors`
still finds *Rumours*), and each candidate is tagged `exact` / `strong` /
`weak`. That tag is the confidence gate — it is why a cheap model is good
enough. `weak` is rejected outright.

**4. A blank beats a guess.** The same rule governs BPM lookup. Search always
returns *something*, so every result is checked against what was asked for
before it is accepted — an unverified match means a misread sleeve silently
gets an unrelated record's tempo, which is worse than an empty column.

**5. The sleeve read is what you are waiting for.** BPM and key are a second
pass, fired once the tracklist is already on screen, six lookups at a time.
Nothing about tempo is allowed to hold up the identification, and nothing about
tempo is allowed to fail it either — every tier below degrades to a blank cell.

```
  photos ──► vision model ──► artist / album / tracklist ──► rendered
                 │                                              │
                 ├─ no tracklist?   ─► Discogs, then MB (by name)│
                 └─ no album name?  ─► model names it            │
                                       ─► Discogs, then MB confirm│
                        strict, then fuzzy, per source            │
                        vinyl pressing preferred                 │
                                                                 ▼
                                       per track:  cache ──► Spotify search
                                                       │          │ (track id)
                                                       │          ▼
                                                       └────► ReccoBeats
                                                                  │
                                                        BPM · Camelot key
```

---

## Files

In the live path:

| File | Role |
|---|---|
| `app.py` | Flask server — page, `/api/scan`, `/api/bpm` |
| `vinyl.py` | The engine — vision call, prompt, fallback orchestration |
| `identify.py` | Discogs and MusicBrainz search, fuzzy matching, vinyl-preference ranking |
| `bpm.py` | BPM/key resolution — cache → Spotify search → ReccoBeats |
| `analyze.py` | Camelot conversion, half/double time, pitch-fader math |
| `templates/index.html` | The dashboard (`/app`) — also talks to Supabase directly for auth and the library |
| `templates/home.html` | The homepage (`/`) |
| `templates/brand.html` | Brand identity reference — palette, type, logo, voice (`/brand`) |
| `supabase/schema.sql` | `albums`, `tracks`, `playlists`, `playlist_tracks` and their RLS policies, run once in your project |
| `supabase/migrations/` | Changes to that schema since — run once each, in order, only if your project predates them |

Standalone, not reachable from the dashboard:

| File | Role |
|---|---|
| `identify.py` (CLI) | `python identify.py photo.jpg` — identify one photo via Gemini directly, without the dashboard |
| `probe.py` | Coverage tester for a BPM/key API, against a crate you list in `crate.txt` |
| `scan.py` | Earlier Gemini-direct version, superseded by `vinyl.py` |

---

## Notes

- Both Discogs and MusicBrainz throttle and occasionally return 429/503 under
  load. This is normal; requests retry with backoff and degrade gracefully
  rather than losing a good scan. Set `MB_USER_AGENT` to something with your
  contact details; Discogs just needs `DISCOGS_TOKEN`.
- Photographing the **back cover** is both faster and more accurate than the
  database fallback — you get that exact pressing's tracklist.
- Sleeves are typeset, so the model reads back real typographic punctuation.
  A curly apostrophe in `Don’t Stop ’til You Get Enough` cuts ReccoBeats'
  search from twenty results to one, so search text is ASCII-folded first.
  Stripping the apostrophe entirely is just as bad — it has to become `'`.
- Spotify enforces a daily quota per app, and a `429` there carries a
  `Retry-After` measured in hours, not seconds. The hop switches itself off for
  that window and lookups fall back to ReccoBeats rather than failing.
- Results are cached in `bpm_cache.sqlite` (gitignored). Hits are permanent;
  misses expire after a week so a track a catalogue learns later is not
  blacklisted forever. Network errors are never cached at all.

## License

MIT
