"""
BPM and key for a track, from the cheapest source that already knows it.

    python bpm.py "Frankie Knuckles" "Your Love"
    python bpm.py --record scan.json          <- annotate a whole tracklist
    python bpm.py --check                     <- are the credentials working?

Chain, cheapest first:

    1. cache      - sqlite, permanent, free
    2. reccobeats - Spotify's audio-features schema, still served, no key needed

Tier 2 needs a Spotify search hop to find the track ID. That hop is not
decoration: ReccoBeats' own /track/search takes a title with no artist term and
cannot find Fleetwood Mac's "Dreams" in fifty results. Spotify's search is what
turns "artist + title read off a sleeve" into an exact ID. Only Spotify's
audio-features endpoint was deprecated; search still works on client credentials.

Slipmat starts from a photograph, so there is no audio to measure and every
number here is looked up rather than heard. That means it is the tempo of *a*
digital master, not of the pressing on your platter at your pitch setting -
a starting point for the fader, not a reading off it. Records that streaming
never carried have no BPM here at all, and the column stays blank.
"""

import base64
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time

import requests

import analyze
import identify

RECCO = "https://api.reccobeats.com/v1"
SPOTIFY_TOKEN = "https://accounts.spotify.com/api/token"
SPOTIFY_SEARCH = "https://api.spotify.com/v1/search"

# ReccoBeats reports key as a pitch class; analyze.to_camelot wants a note name.
PITCH_CLASS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Sleeves are typeset, so the model reads back real typographic punctuation.
# A curly apostrophe cuts ReccoBeats' search from twenty results to one, which
# is how "Don't Stop 'til You Get Enough" goes missing off Off the Wall.
TYPOGRAPHIC = {0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"',
               0x2013: "-", 0x2014: "-", 0x2026: "..."}


def _plain(s):
    """Typographic punctuation -> ASCII, for text going into a search box."""
    return (s or "").translate(TYPOGRAPHIC)


def _duration(seconds):
    """3900 -> '1h 5m'. For Retry-After values that run to hours."""
    seconds = int(seconds)
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % round(seconds / 60)
    return "%dh %dm" % (seconds // 3600, seconds % 3600 // 60)


def _env():
    """Read .env once, lazily - importing vinyl at module scope invites a cycle."""
    import vinyl                      # noqa: F401  (its import calls load_env)


# ------------------------------------------------------------------- the cache

class Cache:
    """Every resolved track is kept forever. Misses expire, in case a source
    later learns the track."""

    MISS_TTL = 7 * 24 * 3600

    def __init__(self, path="bpm_cache.sqlite"):
        self.path = self._writable_path(path)
        # :memory: only means something to the connection that opened it - a
        # fresh connection per operation would see a fresh empty database
        # each time. That one case keeps a single connection alive instead.
        self._mem_con = (sqlite3.connect(":memory:", check_same_thread=False)
                         if self.path == ":memory:" else None)
        self._mem_lock = threading.Lock()

        with self._con() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS features (
                artist TEXT, title TEXT, fetched_at INT, found INT, data TEXT,
                PRIMARY KEY (artist, title))""")
            con.commit()

    @contextlib.contextmanager
    def _con(self):
        """A fresh connection per operation, normally.

        Flask answers requests on a thread pool and a sqlite connection may
        only be used by the thread that opened it - one long-lived connection
        works from the CLI and then throws on the dashboard's second request.
        The in-memory fallback is the one exception: there, "fresh" would
        mean empty, so it reuses a single connection under a lock instead.
        """
        if self._mem_con is not None:
            with self._mem_lock:
                yield self._mem_con
        else:
            with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as con:
                yield con

    @staticmethod
    def _writable_path(path):
        """Next to bpm.py works for local dev, but serverless hosts (Vercel,
        AWS Lambda) ship the source tree read-only - only /tmp is writable
        there, and even that is wiped between cold starts. Try each in turn
        and fall back to a private in-memory database rather than let a
        read-only filesystem take down every lookup.
        """
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), path),
            os.path.join(tempfile.gettempdir(), path),
        ]
        for candidate in candidates:
            try:
                with contextlib.closing(sqlite3.connect(candidate, timeout=10)):
                    pass
            except sqlite3.OperationalError:
                continue
            return candidate

        print("  bpm: no writable location for the cache - "
              "falling back to in-memory (nothing will persist)",
              file=sys.stderr)
        return ":memory:"

    def get(self, artist, title):
        """Returns (features|None, hit). `hit` distinguishes a cached miss from
        never having looked."""
        with self._con() as con:
            row = con.execute(
                "SELECT fetched_at, found, data FROM features "
                "WHERE artist=? AND title=?",
                (identify._norm(artist), identify._norm(title))).fetchone()
        if not row:
            return None, False
        fetched_at, found, data = row
        if not found and time.time() - fetched_at > self.MISS_TTL:
            return None, False
        return (json.loads(data) if found else None), True

    def put(self, artist, title, feat):
        with self._con() as con:
            con.execute(
                "INSERT OR REPLACE INTO features VALUES (?,?,?,?,?)",
                (identify._norm(artist), identify._norm(title), int(time.time()),
                 1 if feat else 0, json.dumps(feat) if feat else ""))
            con.commit()


# ----------------------------------------------------------------- tier 2: net

class Spotify:
    """Search only. Client credentials, no user login, no scopes."""

    def __init__(self, client_id=None, client_secret=None):
        _env()
        self.id = client_id or os.environ.get("SPOTIFY_CLIENT_ID")
        self.secret = client_secret or os.environ.get("SPOTIFY_CLIENT_SECRET")
        self.broken = None          # why the credentials were given up on
        self.retry_at = 0.0         # quota exhausted until this time
        self._token = None
        self._expires = 0.0

    @property
    def present(self):
        """Credentials are filled in, whether or not they currently work."""
        return bool(self.id and self.secret)

    @property
    def configured(self):
        """Usable right now. A typo in .env, or an exhausted quota, should cost
        one warning and a fallback - not one failed lookup per track."""
        return self.present and not self.broken and time.time() >= self.retry_at

    def _note_failure(self, exc):
        """Classify an HTTP error. True means 'stop asking, fall back quietly'.

        Spotify is an optional accelerator here, so nothing it does should be
        able to fail a lookup outright - ReccoBeats' own search still works.
        """
        resp = exc.response
        code = resp.status_code if resp is not None else 0

        if code in (400, 401, 403):
            self.broken = "credentials rejected (HTTP %d)" % code
            print("  bpm: Spotify %s - check SPOTIFY_CLIENT_ID and "
                  "SPOTIFY_CLIENT_SECRET in .env. Falling back to the weaker "
                  "ReccoBeats search." % self.broken, file=sys.stderr)
            return True

        if code == 429:
            # Retry-After on a quota rejection is hours, not seconds.
            wait = float(resp.headers.get("Retry-After") or 60)
            self.retry_at = time.time() + wait
            print("  bpm: Spotify quota exhausted, back in %s. Falling back to "
                  "the weaker ReccoBeats search." % _duration(wait),
                  file=sys.stderr)
            return True

        return False

    def token(self):
        if self._token and time.time() < self._expires - 30:
            return self._token
        if not self.configured:
            raise RuntimeError("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set")

        auth = base64.b64encode(("%s:%s" % (self.id, self.secret)).encode()).decode()
        r = requests.post(SPOTIFY_TOKEN,
                          headers={"Authorization": "Basic " + auth},
                          data={"grant_type": "client_credentials"}, timeout=20)
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._expires = time.time() + float(body.get("expires_in", 3600))
        return self._token

    def _search(self, query, limit):
        r = requests.get(SPOTIFY_SEARCH,
                         headers={"Authorization": "Bearer " + self.token()},
                         params={"q": query, "type": "track", "limit": limit},
                         timeout=20)
        if r.status_code == 401:            # token died early; one retry
            self._token = None
            r = requests.get(SPOTIFY_SEARCH,
                             headers={"Authorization": "Bearer " + self.token()},
                             params={"q": query, "type": "track", "limit": limit},
                             timeout=20)
        r.raise_for_status()
        return r.json().get("tracks", {}).get("items", [])

    def find(self, artist, title, limit=5):
        """Best matching track, or None. Field-scoped query first, then loose.

        A sleeve credits "Larry Heard presents Mr. Fingers" where Spotify has
        only one of those names, so a field-scoped query that returns nothing
        is retried as free text before giving up.
        """
        artist, title = _plain(artist), _plain(title)
        first = (artist or "").split(",")[0].split("/")[0].strip()
        queries = []
        if title and first:
            queries.append('track:"%s" artist:"%s"' % (title, first))
            queries.append("%s %s" % (first, title))
        elif title:
            queries.append(title)

        for query in queries:
            try:
                items = self._search(query, limit)
            except requests.HTTPError as exc:
                if self._note_failure(exc):
                    return None         # handled; the caller falls back
                raise
            hit = _pick(items, artist, title)
            if hit:
                return hit
        return None


def _pick(items, artist, title):
    """Take a Spotify result only if it actually agrees with what we asked for.

    Search always returns *something*; without this a misread sleeve silently
    gets the BPM of an unrelated record, which is worse than no BPM at all.
    """
    want_a, want_t = identify._norm(artist), identify._norm(title)
    for it in items:
        got_a = identify._norm(" ".join(a["name"] for a in it.get("artists", [])))
        got_t = identify._norm(it.get("name"))
        title_ok = want_t and (want_t in got_t or got_t in want_t)
        artist_ok = (not want_a) or any(
            w in got_a for w in want_a.split() if len(w) > 2)
        if title_ok and artist_ok:
            return it
    return None


class Recco:
    """ReccoBeats. Free, no key. Serves the audio-features schema Spotify retired."""

    def __init__(self, min_interval=0.3):
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def _get(self, path, **params):
        # The lock covers the bookkeeping, not the request: that spaces out
        # when calls *start* while still letting several be in flight, so a
        # ten-track LP is not ten round trips end to end.
        with self._lock:
            gap = time.time() - self._last
            wait = max(0.0, self.min_interval - gap)
            self._last = time.time() + wait
        if wait:
            time.sleep(wait)

        r = requests.get(RECCO + path, params=params, timeout=20)
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def by_spotify_id(self, spotify_id):
        body = self._get("/track", ids=spotify_id) or {}
        content = body.get("content") or []
        return content[0] if content else None

    def by_search(self, title, artist, size=20):
        """Fallback when Spotify is not configured. Title-only search, so the
        artist is checked here - and expect misses on common titles."""
        body = self._get("/track/search", searchText=_plain(title), size=size) or {}
        return _pick_recco(body.get("content") or [], artist, title)

    def features(self, recco_id):
        return self._get("/track/%s/audio-features" % recco_id)


def _pick_recco(items, artist, title):
    want_a, want_t = identify._norm(artist), identify._norm(title)
    for it in items:
        got_a = identify._norm(" ".join(a["name"] for a in it.get("artists", [])))
        got_t = identify._norm(it.get("trackTitle"))
        if want_t and (want_t in got_t or got_t in want_t):
            if not want_a or any(w in got_a for w in want_a.split() if len(w) > 2):
                return it
    return None


def _from_recco(feat, track=None):
    """ReccoBeats payload -> the shape analyze.normalise() produces."""
    if not feat or feat.get("tempo") is None:
        return None

    pc, mode = feat.get("key"), feat.get("mode")
    note = PITCH_CLASS[int(pc)] if pc is not None and 0 <= int(pc) <= 11 else None
    bpm = float(feat["tempo"])
    ms = (track or {}).get("durationMs") or 0

    out = {
        "bpm": round(bpm, 1),
        "bpm_alternatives": analyze.tempo_candidates(bpm),
        "key": note,
        "mode": "major" if mode == 1 else "minor" if mode == 0 else None,
        # No mode means no side of the wheel, so no Camelot - not a guessed minor.
        "camelot": analyze.to_camelot(note, mode) if note and mode is not None else None,
        "energy": feat.get("energy"),
        "danceability": feat.get("danceability"),
        "loudness_db": feat.get("loudness"),
        "isrc": feat.get("isrc") or (track or {}).get("isrc"),
        "duration_s": round(ms / 1000) if ms else None,
        "source": "reccobeats",
    }
    if track:
        # Which recording these numbers actually describe. Worth carrying all
        # the way to the screen: a title-only search (all a various-artists
        # compilation can do for a track with no credit of its own) will
        # happily match a modern re-edit of a 1977 song and report its tempo
        # as if it were the pressing's. Naming the recording makes that
        # visible instead of silent.
        out["matched_artist"] = ", ".join(a["name"] for a in track.get("artists", [])) or None
        out["matched"] = "%s - %s" % (out["matched_artist"] or "?",
                                      track.get("trackTitle"))
    return out


# ------------------------------------------------------------------- the chain

_clients_cache = None
_clients_lock = threading.Lock()


def _clients():
    """One set of clients per process, built once even under a thread pool."""
    global _clients_cache
    with _clients_lock:
        if _clients_cache is None:
            _clients_cache = (Cache(), Spotify(), Recco())
    return _clients_cache


def lookup(artist, title, allow_network=True):
    """Features for one track, or None if nothing knows it. Cheapest tier first."""
    if not title:
        return None

    cache, spotify, recco = _clients()
    feat, hit = cache.get(artist, title)
    if hit:
        if feat:
            feat["source"] = "cache(%s)" % feat.get("source", "?")
        return feat
    if not allow_network:
        return None

    found = None
    try:
        track = None
        if spotify.configured:
            sp = spotify.find(artist, title)
            if sp:
                track = recco.by_spotify_id(sp["id"])
        if track is None:
            # No Spotify credentials, or Spotify had it and ReccoBeats did not.
            track = recco.by_search(title, artist)
        if track:
            found = _from_recco(recco.features(track["id"]), track)
    except requests.RequestException as exc:
        # A network blip is not a miss - say so and leave the cache alone.
        print("  bpm: network error (%s) - not cached" % exc, file=sys.stderr)
        return None

    cache.put(artist, title, found)
    return found


def annotate(record, allow_network=True):
    """Fill in every track of a Slipmat record in place, and return it.

    The sleeve gives one artist for the whole release, so that is what each
    track is looked up under. A compilation with per-track artists will miss,
    which is honest - better a blank than another record's BPM.
    """
    artist = record.get("artist")
    for track in record.get("tracks") or []:
        feat = lookup(artist, track.get("title"), allow_network=allow_network)
        if feat:
            track["bpm"] = feat["bpm"]
            track["key"] = feat["camelot"] or feat["key"]
            track["bpm_source"] = feat["source"]
            track["features"] = feat
    return record


# -------------------------------------------------------------------- the cli

# Michael Jackson is not in ReccoBeats' first fifty results for this title, so
# it resolves with the Spotify hop and not without it. That makes it a canary:
# if this one lands, the credentials are doing their job.
CANARY = ("Michael Jackson", "Rock With You")


def check():
    """Report what is configured, and prove it against a known-hard track."""
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    _, spotify, recco = _clients()

    print("\n  .env: %s" % (env if os.path.exists(env) else env + "  (missing)"))

    if not spotify.present:
        print("  spotify: not configured\n"
              "\n    Add these two lines to .env:\n"
              "      SPOTIFY_CLIENT_ID=...\n"
              "      SPOTIFY_CLIENT_SECRET=...\n"
              "\n    Get them free at https://developer.spotify.com/dashboard"
              " - create an app,\n    any name, any redirect URI; the two"
              " values are on its settings page.\n")
    else:
        print("  spotify: client id %s..., secret set" % spotify.id[:6])
        try:
            spotify.token()
            print("  spotify: credentials accepted")
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            print("  spotify: REJECTED (HTTP %d) - check the id and secret" % code)
        except requests.RequestException as exc:
            print("  spotify: could not reach Spotify (%s)" % exc)

    artist, title = CANARY
    print("\n  canary: %s - %s" % (artist, title))
    track = None
    via = "reccobeats search"
    if spotify.configured:
        try:
            sp = spotify.find(artist, title)
            if sp:
                track = recco.by_spotify_id(sp["id"])
                via = "spotify hop"
        except requests.RequestException as exc:
            print("    spotify lookup failed: %s" % exc)
    if track is None:
        track = recco.by_search(title, artist)

    if spotify.retry_at > time.time():
        print("    spotify quota exhausted - the hop is off for %s"
              % _duration(spotify.retry_at - time.time()))

    feat = _from_recco(recco.features(track["id"]), track) if track else None
    if feat:
        print("    %s BPM  %s  via the %s\n"
              % (feat["bpm"], feat.get("camelot") or "?", via))
    elif spotify.configured:
        print("    not found even with the hop - that is a matching problem,"
              " not a credentials one\n")
    else:
        print("    not found - expected while the hop is unavailable\n")


def _show(feat, label):
    if not feat:
        print("\n  %s\n    no BPM or key found for this one\n" % label)
        return

    print("\n  %s" % label)
    if feat.get("matched"):
        print("    matched: %s" % feat["matched"])
    print("    %s BPM   %s   (%s %s)   via %s"
          % (feat["bpm"], feat.get("camelot") or "?",
             feat.get("key") or "?", feat.get("mode") or "", feat["source"]))

    alts = [b for b in feat.get("bpm_alternatives") or [] if b != feat["bpm"]]
    if alts:
        print("    half/double time: %s" % ", ".join(str(b) for b in alts))
    if feat.get("camelot"):
        print("    mixes with: %s"
              % ", ".join(analyze.camelot_neighbours(feat["camelot"])))

    print("    pitch fader (+/-8%):")
    for target in (120, 128, 140):
        ok, pct = analyze.reachable(feat["bpm"], target)
        print("      to %s BPM: %s"
              % (target, "%+.1f%%" % pct if ok else "out of range (%+.1f%%)" % pct))
    print()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    argv = sys.argv[1:]

    if "--check" in argv:
        check()

    elif "--record" in argv:
        rec_path = argv[argv.index("--record") + 1]
        rec = annotate(json.load(open(rec_path, encoding="utf-8")))
        print("\n  %s - %s\n" % (rec.get("artist") or "?", rec.get("album") or "?"))
        for t in rec.get("tracks") or []:
            print("   %-4s %-40s %7s  %-4s %s"
                  % (t.get("position") or "", (t.get("title") or "?")[:40],
                     t.get("bpm") or "-", t.get("key") or "",
                     t.get("bpm_source") or ""))
        print()

    elif len(argv) >= 2:
        _show(lookup(argv[0], argv[1]), "%s - %s" % (argv[0], argv[1]))

    else:
        sys.exit('usage: python bpm.py "Artist" "Title"\n'
                 '       python bpm.py --record scan.json\n'
                 '       python bpm.py --check          (are the credentials working?)')
