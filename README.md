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
- Falls back to MusicBrainz when the photo shows no tracklist
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
never sees a password. A **Save to library** button appears under a scanned
record once signed in; a **Library** tab lists everything saved, with a delete
on each entry. The browser talks to Supabase directly for all of this — Flask
never touches it, so `/api/scan` and `/api/bpm` are unchanged either way.

The Library tab is Spotify's shape on top of Rekordbox's data. **Albums** and
**Artists** browse cover-art grids — Artists is grouped from the same rows,
most recently active first; clicking one filters Albums down to theirs, with
a chip to clear it. **Tracks** is Rekordbox's Collection view — every track
flattened out of its album into one sortable table; click the BPM or Key
header to sort by it, click again to reverse. Key sorts around the Camelot
wheel (`1A, 1B, 2A, 2B, …`), not alphabetically, so `10A` doesn't land ahead
of `2A`. Clicking any album tile, or the **View as list** link, drops into the
full Rekordbox-style detail instead: every track with its BPM and Camelot key,
grouped back under its album, same as the scan results table.

**Playlists** is Rekordbox's own tree: **All Tracks** first (the same flat
table as the Tracks pill — one function renders both), then folders and
playlists underneath, nested arbitrarily deep. Right-click **Playlists** or
any folder for a small panel — pick **New Playlist** or **New Folder**, then
name it right there; no browser `prompt()` dialog, which looked and behaved
like nothing else in this app. A playlist is a leaf, so right-clicking one
gets no menu at all. Unlike Albums/Artists/Tracks — all just different
views over the albums you've saved — playlists are real rows: a track can sit
in any number of them, which is the one place in this schema a join table is
actually the right call (`playlists`, a self-referencing tree of folders and
playlists, and `playlist_tracks`, which track sits in which playlist and in
what order). A playlist's own contents render in that stored order, not
sortable — sorting would defeat the point of a deliberately ordered set.
Drag a row out of **All Tracks** and drop it on a playlist in the tree to add
it there — not onto a folder, which can't hold tracks directly, and not onto
All Tracks itself, which is the read-only aggregate, not a real list.

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
  pill shows a permission/relation error instead of the tree.

---

## How it works

**1. Read, don't recall.** The prompt tells the model to transcribe what is
printed and return `null` for anything illegible. Without that instruction it
pattern-matches a familiar cover and recites a tracklist from training data —
which looks correct and is unverifiable.

**2. Sleeve first, database second.** The record in your hands is ground truth.
A database lookup can only guess *which pressing you own*, and the 1979 12"
has different timings from the 2016 reissue. MusicBrainz is consulted only when
the photos show no tracklist at all.

**3. The recogniser proposes, the database disposes.** When the fallback does
run, the model's output is a hypothesis, never an answer. Results are searched
strictly, then fuzzily (so a misread `Rumors` still finds *Rumours*), and each
candidate is tagged `exact` / `strong` / `weak`. That tag is the confidence
gate — it is why a cheap model is good enough. `weak` is rejected outright.

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
                 └─ no tracklist? ─► MusicBrainz                 │
                        strict, then fuzzy                       │
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
| `identify.py` | MusicBrainz search, fuzzy matching, vinyl-preference ranking |
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

- MusicBrainz throttles and returns 503 under load. This is normal; requests
  retry with backoff and degrade gracefully rather than losing a good scan.
  Set `MB_USER_AGENT` to something with your contact details.
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
