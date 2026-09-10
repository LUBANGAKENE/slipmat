"""
Identify a vinyl record from a photo.

Design principle: the recogniser proposes, the database disposes. The model
produces a *hypothesis* - artist and album strings - and nothing is trusted
until MusicBrainz confirms it against a real release. That is what lets a free
model be good enough.

Setup:
    pip install requests          (PIL, google-generativeai already present)
    $env:GEMINI_API_KEY="..."     from aistudio.google.com, free, no card
"""

import json
import os
import re
import sys
import time
import unicodedata

import requests

# MusicBrainz requires an identifying User-Agent and throttles generic ones.
# Put your own contact address here - they ask for it, and it buys goodwill
# if you ever hit their rate limiter.
UA = os.environ.get("MB_USER_AGENT", "Slipmat/0.1 (https://github.com/LUBANGAKENE/slipmat)")
MB = "https://musicbrainz.org/ws/2"
_last_mb = 0.0


# ------------------------------------------------------------------ the model

PROMPT = """You are looking at a photograph of a vinyl record - either its sleeve
or its centre label.

Read the text visible in the image and report what release it is. Do not guess
from the artwork style; if the text is unreadable, say so.

Return ONLY minified JSON, no markdown fence:
{"artist": str|null, "album": str|null, "catalog_number": str|null,
 "label": str|null, "track_titles": [str], "confidence": "high"|"medium"|"low",
 "reasoning": str}

catalog_number is the alphanumeric code printed on the label or sleeve spine
(e.g. "BRC-441", "SW-11163"). It identifies the exact pressing, so include it
whenever visible. track_titles only if a tracklist is legible on the back."""


def gemini_identify(path, api_key=None, model="gemini-2.5-flash"):
    """Free-tier vision call. Returns a hypothesis dict, not a fact."""
    import google.generativeai as genai
    from PIL import Image

    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set - free at aistudio.google.com")
    genai.configure(api_key=key)

    img = Image.open(path)
    img.thumbnail((1024, 1024))          # plenty for OCR, less quota burned

    resp = genai.GenerativeModel(model).generate_content([PROMPT, img])
    text = (resp.text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"artist": None, "album": None, "confidence": "low",
                "reasoning": "unparseable model output: " + text[:200]}


# ------------------------------------------------------------------ the verifier

class DatabaseUnavailable(RuntimeError):
    """A release database is throttling or down. Transient - not a failed lookup."""


class MusicBrainzUnavailable(DatabaseUnavailable):
    """MusicBrainz is throttling or down. Transient - not a failed lookup."""


class DiscogsUnavailable(DatabaseUnavailable):
    """Discogs is throttling or down, or DISCOGS_TOKEN isn't set."""


def _mb_get(path, attempts=3, **params):
    """MusicBrainz is 1 req/sec and wants a real User-Agent. Be a good citizen.

    A 429/503 (throttled) or a 502/504 (their gateway having a moment) means
    "not now", not "no such release" - so those are retried with backoff and
    then raised as MusicBrainzUnavailable, which every caller degrades around.
    Only a genuine 4xx like 404 falls through as a hard error.
    """
    global _last_mb
    params["fmt"] = "json"
    delay = 2.0

    for attempt in range(attempts):
        gap = time.time() - _last_mb
        if gap < 1.1:
            time.sleep(1.1 - gap)

        try:
            r = requests.get(MB + "/" + path, params=params,
                             headers={"User-Agent": UA}, timeout=20)
        except requests.RequestException as exc:
            if attempt == attempts - 1:
                raise MusicBrainzUnavailable(str(exc))
            time.sleep(delay)
            delay *= 2
            continue
        finally:
            _last_mb = time.time()

        if r.status_code in (429, 502, 503, 504):
            if attempt == attempts - 1:
                raise MusicBrainzUnavailable(
                    "MusicBrainz returned %d after %d attempts"
                    % (r.status_code, attempts))
            wait = float(r.headers.get("Retry-After") or delay)
            time.sleep(min(wait, 10.0))
            delay *= 2
            continue

        r.raise_for_status()
        return r.json()

    raise MusicBrainzUnavailable("exhausted retries")


def _norm(s):
    """Fold accents, punctuation and case so Bjork matches the accented spelling."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _release(r):
    credit = r.get("artist-credit") or []
    return {
        "source": "musicbrainz",
        "id": r.get("id"),
        "mbid": r.get("id"),          # kept for callers that still read it directly
        "title": r.get("title"),
        "artist": " ".join(c.get("name", "") for c in credit).strip(),
        "date": r.get("date"),
        "country": r.get("country"),
        "format": ", ".join(m.get("format", "?") for m in (r.get("media") or [])),
        "catno": ", ".join(li.get("catalog-number", "")
                           for li in (r.get("label-info") or [])
                           if li.get("catalog-number")),
        "score": r.get("score", 0),
        "match": None,
    }


def _fuzzy(field, value, edits=1):
    """Lucene fuzzy term: rumours -> release:(rumours~1). Survives OCR slips."""
    words = [w for w in re.split(r"\W+", value or "") if len(w) > 1]
    if not words:
        return None
    return "%s:(%s)" % (field, " ".join("%s~%d" % (w, edits) for w in words))


_GENERIC_ARTISTS = {"various artists", "various", "va", "v/a",
                    "diverse artister", "olika artister", "unknown artist"}


def is_generic_artist(name):
    """True for a various-artists placeholder ("Various Artists", "VA", ...)
    rather than an actual performer's name. Used well beyond this module: a
    placeholder like this is worse than no artist at all as a search term -
    it constrains a query to an artist that doesn't exist in the catalogue
    being searched, rather than leaving the field open.
    """
    return _norm(name) in _GENERIC_ARTISTS


def _strip_parens(s):
    """Drop trailing "(The Original Movie Sound Track)"-style annotations.

    A release's canonical Discogs title often carries a parenthetical
    subtitle that nothing printed on a sleeve, or transcribed off one, is
    going to reproduce word for word. Without stripping it, "Saturday Night
    Fever" scores only a weak loose-containment match against Discogs'
    "Saturday Night Fever (The Original Movie Sound Track)" - worse than an
    unrelated release whose title happens to match exactly.
    """
    return re.sub(r"\s*[\(\[][^()\[\]]*[\)\]]\s*$", "", s or "").strip()


def _rank(candidates, artist, album):
    """Score-fill 'match' on each candidate: how well artist/title agree with
    the hypothesis, independent of whichever source's own relevance score.
    Shared by every source so "exact"/"strong"/"weak" mean the same thing
    whether the candidate came from Discogs or MusicBrainz.

    "Various Artists" is a special case: MusicBrainz tags a large fraction of
    all compilations with exactly that string, so an artist match there is
    nearly free and proves nothing. Without this, a short generic album title
    like "Dancing Queen" scores "strong" against a completely unrelated CD
    compilation just because it's a substring of the real, longer title and
    both happen to say "Various Artists". Generic artists are excluded from
    the score entirely, so a compilation is only accepted on the strength of
    its title.
    """
    want_a, want_t = _norm(artist), _norm(album)
    generic = is_generic_artist(artist)
    for rel in candidates:
        got_a, got_t = _norm(rel["artist"]), _norm(rel["title"])
        title_exact = got_t == want_t or _norm(_strip_parens(rel["title"])) == want_t
        exact = (0 if generic else got_a == want_a) + title_exact
        loose = ((0 if generic else (want_a in got_a or got_a in want_a)) +
                 (want_t in got_t or got_t in want_t))
        rel["match"] = ("exact" if exact == 2
                        else "strong" if exact + loose >= 2
                        else "weak")
    return candidates


def _mb_search(artist, album, catalog_number=None):
    """MusicBrainz's own verifier, used as the fallback behind Discogs.

    Two passes. Strict phrases first, because when they hit they are right.
    Then a fuzzy pass, because vision and OCR misread characters constantly
    and an exact-match-only verifier throws away every near miss.

    A generic hypothesis artist ("Various Artists" and its spellings) is
    left out of the query entirely, not just the ranking - constraining the
    search by a label that's really a placeholder only loses recall, since
    a database might credit the same release to "Unknown Artist" or nothing
    at all.
    """
    generic = is_generic_artist(artist)

    strict = []
    if album:
        strict.append('release:"%s"' % album)
    if artist and not generic:
        strict.append('artist:"%s"' % artist)
    if catalog_number:
        strict.append('catno:"%s"' % catalog_number)
    if not strict:
        return []

    data = _mb_get("release", query=" AND ".join(strict), limit=10)
    releases = data.get("releases", [])

    if not releases:
        loose = [q for q in (_fuzzy("release", album),
                             None if generic else _fuzzy("artist", artist)) if q]
        if loose:
            data = _mb_get("release", query=" AND ".join(loose), limit=10)
            releases = data.get("releases", [])

    out = _rank([_release(r) for r in releases], artist, album)

    # Prefer vinyl pressings. A CD release numbers its tracks 1,2,3 - only the
    # vinyl carries the A1/B1 side positions that tell you where to drop the
    # needle, which is the whole reason we're looking this up.
    order = {"exact": 0, "strong": 1, "weak": 2}
    return sorted(out, key=lambda r: (order[r["match"]],
                                      0 if "vinyl" in (r["format"] or "").lower() else 1,
                                      -r["score"]))


# ------------------------------------------------------------------ Discogs

DISCOGS = "https://api.discogs.com"
DISCOGS_UA = os.environ.get(
    "DISCOGS_USER_AGENT", "Slipmat/0.1 +https://github.com/LUBANGAKENE/slipmat")
_last_discogs = 0.0


def _discogs_get(path, attempts=3, **params):
    """Discogs allows 60 authenticated requests/minute. Space calls out and
    treat 429/5xx as transient, the same way _mb_get treats MusicBrainz's
    limiter. A 404 means "no such release", not "try again" - return None
    for the caller to handle rather than raising.
    """
    global _last_discogs
    token = os.environ.get("DISCOGS_TOKEN")
    if not token:
        raise DiscogsUnavailable("DISCOGS_TOKEN not set")
    params["token"] = token
    delay = 2.0

    for attempt in range(attempts):
        gap = time.time() - _last_discogs
        if gap < 1.1:
            time.sleep(1.1 - gap)

        try:
            r = requests.get(DISCOGS + "/" + path.lstrip("/"), params=params,
                             headers={"User-Agent": DISCOGS_UA}, timeout=20)
        except requests.RequestException as exc:
            if attempt == attempts - 1:
                raise DiscogsUnavailable(str(exc))
            time.sleep(delay)
            delay *= 2
            continue
        finally:
            _last_discogs = time.time()

        if r.status_code in (429, 502, 503, 504):
            if attempt == attempts - 1:
                raise DiscogsUnavailable(
                    "Discogs returned %d after %d attempts"
                    % (r.status_code, attempts))
            wait = float(r.headers.get("Retry-After") or delay)
            time.sleep(min(wait, 10.0))
            delay *= 2
            continue

        if r.status_code == 404:
            return None

        if r.status_code in (401, 403):
            # Bad, revoked, or malformed token. Retrying won't fix that, and
            # this must degrade to "fall back to MusicBrainz", not crash the
            # whole scan the way an unhandled HTTPError would.
            raise DiscogsUnavailable(
                "Discogs rejected the token (%d) - check DISCOGS_TOKEN"
                % r.status_code)

        try:
            r.raise_for_status()
        except requests.HTTPError as exc:
            raise DiscogsUnavailable(str(exc))
        return r.json()

    raise DiscogsUnavailable("exhausted retries")


def _discogs_artists(entry):
    """Track-level artists. Discogs disambiguates same-named artists with a
    trailing "(2)", "(3)" - strip that, it's a database artifact, not
    something anyone printed on a sleeve."""
    return ", ".join(re.sub(r"\s*\(\d+\)$", "", a.get("name", "")).strip()
                     for a in (entry.get("artists") or []) if a.get("name")) or None


def _discogs_position(raw, parts):
    """Normalise a Discogs position to the "A1"/"B1" form a sleeve prints.

    Two shapes need fixing. A side holding a single track is written as a
    bare "B", and a medley's parent row carries no position at all - only
    its parts do ("A1a".."A1d"), so the side is taken from what they agree
    on.
    """
    pos = (raw or "").strip()
    if pos:
        return pos + "1" if re.fullmatch(r"[A-Za-z]", pos) else pos

    stems = {m.group(1) for m in
             (re.match(r"([A-Za-z]+\d+)", p.get("position") or "") for p in parts)
             if m}
    return stems.pop() if len(stems) == 1 else None


def _discogs_tracks(tracklist):
    """A Discogs tracklist -> our track dicts.

    Discogs marks a medley or megamix as type_ "index", with the songs mixed
    inside it nested in "sub_tracks". Those are not separate cue points - the
    side plays as one continuous track - so they stay attached to their
    parent as "parts" rather than becoming rows of their own, which would
    promise four places to drop the needle where the record has one. The
    parent itself is a real track and must not be dropped, which is what
    filtering to type_ "track" alone used to do.
    """
    out = []
    for t in tracklist or []:
        if not t.get("title") or t.get("type_") not in (None, "track", "index"):
            continue        # headings and other non-track rows

        parts = [{"position": s.get("position") or None,
                  "title": s.get("title"),
                  "artist": _discogs_artists(s),
                  "duration": s.get("duration") or None}
                 for s in (t.get("sub_tracks") or []) if s.get("title")]

        out.append({"position": _discogs_position(t.get("position"), parts),
                    "title": t.get("title"),
                    "artist": _discogs_artists(t),
                    "duration": t.get("duration") or None,
                    # Discogs' own signal that this row is a continuous mix
                    # holding other songs, not a single recording. Worth
                    # carrying: it's why the track's own tempo can't be
                    # looked up, and why its parts' tempos are the useful
                    # answer instead.
                    "is_mix": t.get("type_") == "index",
                    "parts": parts or None})
    return out


def _discogs_release(r):
    """A /database/search hit -> our common candidate shape. Discogs folds
    artist and title into one "Artist - Title" string; split it back apart."""
    artist, sep, album = (r.get("title") or "").partition(" - ")
    if not sep:
        artist, album = "", artist

    catno = r.get("catno")
    if catno and catno.lower() == "none":
        catno = None

    return {
        "source": "discogs",
        "id": r.get("id"),
        "mbid": None,
        "title": album.strip(),
        "artist": artist.strip(),
        "date": str(r["year"]) if r.get("year") else None,
        "country": r.get("country"),
        "format": ", ".join(r.get("format") or []),
        "catno": catno,
        "score": 0,
        "match": None,
        # How many Discogs users own this exact pressing. Not a quality
        # signal in general - a rare pressing isn't wrong for being rare -
        # only a tiebreaker between two candidates that already score the
        # same on title/artist agreement, where it separates a widely-known
        # release from an obscure same-titled impostor.
        "popularity": (r.get("community") or {}).get("have") or 0,
    }


def discogs_search(artist, album, catalog_number=None):
    """Discogs' catalogue of individual pressings, vinyl only - that's the
    whole reason to consult it over MusicBrainz. Raises DiscogsUnavailable
    when there's no token or the API is down; the caller decides whether
    that's fatal or just means "fall back to the next source".

    Uses the single freeform "q" field rather than Discogs' structured
    "artist" + "release_title" params. The structured combination is broken
    in practice: passing both together silently returns zero results for
    plenty of ordinary releases (verified on Snap!'s "Mega Mix" 12", which
    each field finds fine alone) - a bug on Discogs' side, not a data gap,
    but one this app can only work around, not fix. The same freeform query
    a human would type into Discogs' own search box doesn't have the
    problem, so that's what this sends.

    A generic hypothesis artist ("Various Artists" and its spellings) is
    left out of the query - Discogs credits the same kind of release to
    "Unknown Artist", "Various", or nothing at all, so constraining the
    search by our placeholder only loses recall for no accuracy gain (see
    _rank, which already excludes it from the score).
    """
    generic = is_generic_artist(artist)
    terms = [t for t in (None if generic else artist, album, catalog_number) if t]
    if not terms:
        return []

    params = {"type": "release", "format": "Vinyl", "per_page": 25,
             "q": " ".join(terms)}
    data = _discogs_get("database/search", **params)
    results = (data or {}).get("results") or []

    if not results and catalog_number:
        # A misread catalogue number kills every hit; retry without it.
        params["q"] = " ".join(t for t in terms if t != catalog_number)
        data = _discogs_get("database/search", **params)
        results = (data or {}).get("results") or []

    out = _rank([_discogs_release(r) for r in results], artist, album)

    # Discogs lists every pressing separately, and their tracklists genuinely
    # differ - a 7" edit is not the 12" version. When the sleeve gave a
    # catalogue number, the pressing carrying it is the record in your hands,
    # so it outranks an equally-good title match on some other pressing.
    #
    # Below that, popularity breaks ties among same-tier matches. A generic
    # movie-tie-in title like "Saturday Night Fever" was reused by budget
    # cover-version LPs that title-match exactly, the same way the real
    # soundtrack does once _rank looks past its "(The Original Movie Sound
    # Track)" subtitle - so an exact title match alone doesn't disambiguate
    # them. Discogs' own "have" count does: the genuine soundtrack sits in
    # tens of thousands of collections, an obscure same-titled knockoff in a
    # few hundred.
    want_catno = _norm(catalog_number)
    order = {"exact": 0, "strong": 1, "weak": 2}
    return sorted(out, key=lambda r: (order[r["match"]],
                                      0 if want_catno and _norm(r["catno"]) == want_catno else 1,
                                      -r["popularity"]))


# ------------------------------------------------------------------ merged lookup

def verify_text(artist, album, catalog_number=None):
    """Turn a model hypothesis into confirmed releases, best match first.

    Discogs is tried first when DISCOGS_TOKEN is set - its catalogue of
    individual vinyl pressings runs deeper than MusicBrainz's for the records
    this app actually scans, catalogue number and all. MusicBrainz is
    consulted as a fallback: whenever Discogs isn't configured, has nothing
    better than a weak match, or is down. A source being unavailable is not
    fatal by itself; this only raises DatabaseUnavailable when every source
    that was tried failed and none produced a hit.
    """
    hits = []
    attempted = failed = 0

    if os.environ.get("DISCOGS_TOKEN"):
        attempted += 1
        try:
            hits = discogs_search(artist, album, catalog_number)
        except DiscogsUnavailable:
            failed += 1

    if not hits or hits[0]["match"] == "weak":
        attempted += 1
        try:
            mb_hits = _mb_search(artist, album, catalog_number)
        except MusicBrainzUnavailable:
            failed += 1
            mb_hits = []

        order = {"exact": 0, "strong": 1, "weak": 2, None: 3}
        if not hits:
            hits = mb_hits
        elif mb_hits and order[mb_hits[0]["match"]] < order[hits[0]["match"]]:
            hits = mb_hits + hits

    if not hits and attempted and failed == attempted:
        raise DatabaseUnavailable(
            "every configured release database is unavailable")

    return hits


def resolve_release(candidate):
    """The full release for a verify_text candidate - tracklist, label, and
    catalogue number - fetched from whichever database it came from. Callers
    (vinyl.py) don't need to know or care which that was.
    """
    if candidate["source"] == "discogs":
        data = _discogs_get("releases/%s" % candidate["id"]) or {}
        labels = data.get("labels") or []
        catno = next((l["catno"] for l in labels
                     if l.get("catno") and l["catno"].lower() != "none"), None)
        images = data.get("images") or []
        cover = next((im for im in images if im.get("type") == "primary"), None)
        cover = cover or (images[0] if images else None)
        cover_url = (cover or {}).get("uri") or data.get("thumb") or None
        tracks = _discogs_tracks(data.get("tracklist"))
        return {
            "tracks": tracks,
            "label": labels[0].get("name") if labels else None,
            "catalog_number": catno or candidate.get("catno"),
            "year": str(data["year"]) if data.get("year") else candidate.get("date"),
            "cover_url": cover_url,
        }

    data = _mb_get("release/%s" % candidate["id"],
                   inc="recordings labels artist-credits")
    release_artist = " ".join(c.get("name", "")
                              for c in (data.get("artist-credit") or [])).strip()
    tracks = []
    for medium in data.get("media", []):
        for t in medium.get("tracks", []):
            ms = t.get("length")
            # A various-artists release carries its own artist-credit per
            # track that differs from the release's; a normal release
            # repeats the same one on every track, which is noise here.
            credit = t.get("artist-credit") or []
            track_artist = " ".join(c.get("name", "") for c in credit).strip()
            tracks.append({
                "position": t.get("number"),
                "title": t.get("title"),
                "artist": track_artist if track_artist and track_artist != release_artist else None,
                "duration": ("%d:%02d" % (ms // 60000, ms // 1000 % 60)) if ms else None,
                "is_mix": False,
                "parts": None,      # MusicBrainz has no medley breakdown
            })
    catno = ", ".join(li.get("catalog-number", "")
                      for li in (data.get("label-info") or [])
                      if li.get("catalog-number"))
    return {
        "tracks": tracks,
        "label": next((li["label"]["name"] for li in (data.get("label-info") or [])
                       if (li.get("label") or {}).get("name")), None),
        "catalog_number": catno or candidate.get("catno"),
        "year": data.get("date") or candidate.get("date"),
        # MusicBrainz releases don't carry an image URL in this response -
        # the Cover Art Archive is a separate API this doesn't call.
        "cover_url": None,
    }


def mb_track_artists(artist, album, catalog_number=None):
    """Per-track artist from MusicBrainz, for when Discogs has the tracklist
    but none of its tracks carry their own artist - an occasional gap for a
    less-documented compilation, not every one Discogs holds. Matched by
    normalised title; returns {} if MusicBrainz has nothing usable either.

    Deliberately narrow: only the artist name is trusted from MusicBrainz
    here - see fill_tracklist for why its tracklist itself (which tracks
    exist, their positions, their durations) isn't. A wrong or missing
    artist name is a small miss; this is never used to supply a track
    Discogs didn't already give, only to label one that's already there.
    """
    try:
        hits = _mb_search(artist, album, catalog_number)
    except MusicBrainzUnavailable:
        return {}
    if not hits or hits[0]["match"] == "weak":
        return {}
    try:
        full = resolve_release(hits[0])
    except MusicBrainzUnavailable:
        return {}
    return {_norm(t["title"]): t["artist"]
           for t in full.get("tracks") or [] if t.get("artist") and t.get("title")}


# ------------------------------------------------------------------ the pipeline

def identify(path):
    """Photo in, confirmed release candidates out."""
    trace = []

    guess = gemini_identify(path)
    trace.append("gemini: %s - %s (catno=%s, conf=%s)" % (
        guess.get("artist"), guess.get("album"),
        guess.get("catalog_number"), guess.get("confidence")))

    hits = verify_text(guess.get("artist"), guess.get("album"),
                       guess.get("catalog_number"))
    if not hits and guess.get("catalog_number"):
        # Catalogue numbers are misread often; retry on artist/title alone.
        trace.append("no hit with catno, retrying without it")
        hits = verify_text(guess.get("artist"), guess.get("album"))

    return {"method": "gemini", "hypothesis": guess,
            "confidence": hits[0]["match"] if hits else "none",
            "candidates": hits, "trace": trace}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python identify.py <photo.jpg>")

    res = identify(sys.argv[1])

    print("\n  trace:")
    for t in res["trace"]:
        print("    - " + t)

    cands = res["candidates"]
    print("\n  method=%s  confidence=%s  (%d candidates)\n"
          % (res["method"], res["confidence"], len(cands)))
    for c in cands[:5]:
        print("    [%-6s] %s - %s" % (c["match"], c["artist"], c["title"]))
        print("             %s %s | %s | catno %s"
              % (c["date"] or "?", c["country"] or "", c["format"],
                 c["catno"] or "-"))
        print("             %s id %s" % (c["source"], c["id"]))
