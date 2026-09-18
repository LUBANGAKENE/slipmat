"""
Core: photographs of a record in, structured release info out.

Vision runs through OpenRouter so the model is a one-line config change.
The tracklist is read straight off the back sleeve; Discogs (then, as a
fallback, MusicBrainz) is consulted only when the photos do not show one -
or, when the sleeve names no album, to confirm the release the model
identifies from the tracklist. See identify.verify_text.
"""

import base64
import json
import os
import re
import sys
import time

import requests

API = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-2.5-flash"

COVER_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cover_picks.log")


def _log_cover_pick(entry):
    """Append one JSON line per pick_cover() call - candidates offered, the
    model's raw answer, and what got saved - so a wrong pick can be traced
    back to whether the candidates were bad or the model's read was.
    """
    entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with open(COVER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print("[cover_pick] " + line, file=sys.stderr)


def load_env(path=".env"):
    """Minimal .env reader so the key never has to live in the source."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(here):
        return
    for line in open(here, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


load_env()

PROMPT = """These photographs show a vinyl record - its front sleeve, back
cover, and/or centre label.

Read the printed text and report what this release is. Read what is actually
there; do not infer from artwork style and do not complete titles from memory.
If something is not legible, use null rather than guessing.

Return ONLY minified JSON, no markdown fence:
{"artist": str|null,
 "album": str|null,
 "year": str|null,
 "label": str|null,
 "catalog_number": str|null,
 "tracks": [{"position": str|null, "title": str, "artist": str|null, "duration": str|null}],
 "confidence": "high"|"medium"|"low",
 "notes": str}

"artist" (release-level) is the credited recording artist. If this is a
various-artists release - a hits compilation, a label sampler, a covers album
played by uncredited session musicians - and no single performer is named on
the sleeve, set it to "Various Artists" rather than null. Reserve null for a
single-artist record whose name you genuinely could not read. If the sleeve
states the tracks are re-recordings or not by the original artists, say so in
"notes".

A track's own "artist" is only needed when it differs from the release-level
one - almost always a various-artists compilation, where each song is by a
different original performer and the sleeve prints that name beside the
title. Leave it null on an ordinary single-artist release; the release-level
"artist" already covers every track there, and repeating it on each one is
noise.

"position" is the side-and-index marking printed beside each track: always the
"A1", "A2", "B1" form - translate any language's side wording ("Sida 1",
"Seite 1", "Face A", "Lato A") to that. Preserve the printed order. If sides
are shown but tracks are not individually numbered, number them yourself in
printed order. If no side is shown at all, split the list down the middle into
an A side and a B side.

"duration" only when a running time is printed beside the track.
"notes" should mention anything unreadable or ambiguous, briefly.
If no tracklist is visible in any photo, return "tracks": []."""


def _data_url(raw, mime="image/jpeg"):
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())


def scan_images(images, api_key=None, model=None, timeout=120):
    """images: list of raw bytes, or data: URL strings. Returns a record dict."""
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set (put it in .env)")
    model = model or os.environ.get("VINYL_MODEL") or DEFAULT_MODEL

    parts = [{"type": "text", "text": PROMPT}]
    for im in images:
        url = im if isinstance(im, str) else _data_url(im)
        parts.append({"type": "image_url", "image_url": {"url": url}})

    r = requests.post(
        API,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "X-Title": "Slipmat"},
        json={"model": model, "temperature": 0,
              "messages": [{"role": "user", "content": parts}]},
        timeout=timeout,
    )

    body = r.json()
    if r.status_code != 200 or "choices" not in body:
        msg = (body.get("error") or {}).get("message") or str(body)[:300]
        raise RuntimeError("OpenRouter %s: %s" % (r.status_code, msg))

    text = body["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        rec = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError("model returned unparseable JSON: " + text[:300])

    rec["_model"] = body.get("model", model)
    rec["_usage"] = body.get("usage", {})
    return rec


def scan_paths(paths, **kw):
    """Convenience wrapper for files on disk."""
    images = []
    for p in paths:
        if not os.path.exists(p):
            raise FileNotFoundError(p)
        images.append(open(p, "rb").read())
    return scan_images(images, **kw)


COVER_PROMPT = """The photos labelled "Sleeve" above are photographs of one
vinyl record's actual sleeve, taken by the record's owner - typically front
and back, sometimes just one side. Each photo after those is a candidate
cover image from a music catalogue, labelled with a letter. Catalogue covers
are always the front artwork, so judge each candidate against whichever
sleeve photo is the front - a plain, text-only back cover is not a mismatch,
it's simply not the side to compare.

Compare the candidates against the sleeve photos and decide which one, if
any, is the same cover - the same photograph or artwork, not just the same
album title. A different pressing or a reissue commonly carries completely
different artwork under an identical title; a same-titled but
different-looking candidate is not a match, no matter how confident its
metadata looked.

Return ONLY minified JSON, no markdown fence:
{"match": "A"|"B"|... |null, "confidence": "high"|"medium"|"low"}

Use null if none of the candidates show the same cover as the sleeve's front
- that is a normal answer, not a failure, and is better than picking the
closest-looking wrong one."""

_LETTERS = "ABCDEFGH"


def pick_cover(sleeve_images, candidates, api_key=None, model=None, timeout=45):
    """Which candidate cover (if any) is a photograph of the record actually
    in hand - see identify.cover_candidates and identify.itunes_cover for
    where these come from. Text ranking (artist/album/catalogue number)
    regularly can't tell same-artist releases or same-titled pressings apart;
    looking at the actual photographed sleeve can. Returns a cover_url, or
    None when the model found no confident match - which stays blank rather
    than guessing, the same call this app makes everywhere else.

    Skipped by the caller (see _resolve_cover) whenever there's only one
    candidate to begin with - nothing to compare, so nothing to ask.
    """
    cand_log = [{"letter": _LETTERS[i], "source": c["source"], "id": c.get("id"),
                 "label": c.get("label"), "cover_url": c.get("cover_url")}
                for i, c in enumerate(candidates)]

    if not candidates:
        return None
    if len(candidates) == 1:
        _log_cover_pick({"n_sleeve_images": len(sleeve_images), "candidates": cand_log,
                          "outcome": "single-candidate, no ask",
                          "picked": candidates[0]["cover_url"]})
        return candidates[0]["cover_url"]

    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        _log_cover_pick({"n_sleeve_images": len(sleeve_images), "candidates": cand_log,
                          "outcome": "no API key, fell back to top-ranked",
                          "picked": candidates[0]["cover_url"]})
        return candidates[0]["cover_url"]   # can't ask - the old top-ranked guess

    parts = []
    for img in sleeve_images:
        parts.append({"type": "text", "text": "Sleeve:"})
        parts.append({"type": "image_url", "image_url": {"url": img
                      if isinstance(img, str) else _data_url(img)}})
    for letter, cand in zip(_LETTERS, candidates):
        parts.append({"type": "text",
                      "text": "Candidate %s (%s):" % (letter, cand["label"])})
        parts.append({"type": "image_url", "image_url": {"url": cand["cover_url"]}})
    parts.append({"type": "text", "text": COVER_PROMPT})

    try:
        r = requests.post(
            API,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json", "X-Title": "Slipmat"},
            json={"model": model or os.environ.get("VINYL_MODEL") or DEFAULT_MODEL,
                  "temperature": 0,
                  "messages": [{"role": "user", "content": parts}]},
            timeout=timeout,
        )
        body = r.json()
        if r.status_code != 200 or "choices" not in body:
            _log_cover_pick({"n_sleeve_images": len(sleeve_images), "candidates": cand_log,
                              "outcome": "HTTP %s / no choices" % r.status_code,
                              "raw_body": body, "picked": candidates[0]["cover_url"]})
            return candidates[0]["cover_url"]
        text = body["choices"][0]["message"]["content"].strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        result = json.loads(text)
    except (requests.RequestException, json.JSONDecodeError) as exc:
        _log_cover_pick({"n_sleeve_images": len(sleeve_images), "candidates": cand_log,
                          "outcome": "request/parse failed: %r" % exc,
                          "picked": candidates[0]["cover_url"]})
        return candidates[0]["cover_url"]   # ask failed - fall back, don't block the scan

    match = (result.get("match") or "").strip().upper()[:1]
    if not match:
        _log_cover_pick({"n_sleeve_images": len(sleeve_images), "candidates": cand_log,
                          "model_raw": text, "outcome": "model said no match",
                          "picked": None})
        return None   # the model looked and found none - trust that over a guess
    idx = _LETTERS.find(match)
    picked = candidates[idx]["cover_url"] if 0 <= idx < len(candidates) else None
    _log_cover_pick({"n_sleeve_images": len(sleeve_images), "candidates": cand_log,
                      "model_raw": text, "outcome": "matched %s" % match,
                      "picked": picked})
    return picked


def _resolve_cover(rec, hits, images, model=None):
    """The one place cover_url gets set, for all three callers below (a
    sleeve tracklist, a guessed one, or none at all). Gathers candidates from
    every source that has this release - the top few off `hits` (Discogs,
    or MusicBrainz's own hits, though MusicBrainz never carries a cover
    itself) plus one more from iTunes - then asks pick_cover to look at the
    actual photographed sleeve rather than trust text ranking alone.

    images may be None (no photos on hand, e.g. a hypothetical text-only
    caller) - falls back to the top-ranked candidate's cover without asking,
    same as before this existed.
    """
    import identify

    candidates = identify.cover_candidates(hits)
    itunes = identify.itunes_cover(rec.get("artist"), rec.get("album"))
    if itunes and not any(c["cover_url"] == itunes["cover_url"] for c in candidates):
        candidates.append(itunes)

    if not candidates:
        return
    if not images or len(candidates) == 1:
        rec["cover_url"] = candidates[0]["cover_url"]
        return
    rec["cover_url"] = pick_cover(images, candidates, model=model)


def fill_tracklist(rec, images=None, model=None):
    """Only used when the photos showed no tracklist. Free beyond the cover
    lookup below, needs no OpenRouter key for the tracklist itself.

    The tracklist itself only ever comes from Discogs. It's built around the
    exact vinyl pressing - A1/B1 side positions, per-format track order -
    where MusicBrainz's release-group model blends data across pressings and
    formats loosely enough that its tracklist isn't reliable for "where to
    drop the needle", the whole reason this app exists. Year, label, and
    catalogue number use whichever source matched, Discogs preferred - a
    wrong pressing's year or label is a small miss, a wrong pressing's
    tracklist actively sends you to the wrong track. The cover gets its own
    check, since text agreement on all three still doesn't guarantee the
    artwork does - see _resolve_cover.
    """
    import identify

    hits = identify.verify_text(rec.get("artist"), rec.get("album"),
                                rec.get("catalog_number"))
    if not hits and rec.get("catalog_number"):
        hits = identify.verify_text(rec.get("artist"), rec.get("album"))
    if not hits or hits[0]["match"] == "weak":
        return rec, None

    best = hits[0]
    full = identify.resolve_release(best)
    if not rec.get("year"):
        rec["year"] = full.get("year")
    if not rec.get("label"):
        rec["label"] = full.get("label")
    if not rec.get("catalog_number"):
        rec["catalog_number"] = full.get("catalog_number")
    _resolve_cover(rec, hits, images, model)

    discogs_hits = [h for h in hits if h["source"] == "discogs"]
    if not discogs_hits or discogs_hits[0]["match"] == "weak":
        return rec, None   # metadata confirmed, but no Discogs tracklist to use

    best = discogs_hits[0]
    if best is not hits[0]:
        full = identify.resolve_release(best)   # re-fetch: hits[0] was MusicBrainz
    rec["tracks"] = full["tracks"]

    # Discogs gave the tracklist but sometimes nobody's annotated who
    # performs which song - a gap in Discogs' own data. MusicBrainz
    # occasionally has just that; only worth asking for a various-artists
    # release, where an ordinary album's tracks correctly have no artist
    # of their own.
    if (rec.get("artist") or "").strip().lower() in _VARIOUS:
        _backfill_artists_from_musicbrainz(
            rec["tracks"], rec.get("artist"), rec.get("album"), rec.get("catalog_number"))

    return rec, best


GUESS_PROMPT = """This is the tracklist read off a vinyl record whose sleeve
never prints the album title or the artist - a plain white label, or a cover
that is all artwork. These are real songs. Name the album and the recording
artist they are from.

Tracklist:
%s

Return ONLY minified JSON, no fence:
{"artist": str|null, "album": str|null, "confidence": "high"|"medium"|"low"}

Use null if you genuinely cannot place it. Do not offer a plausible-sounding
name you are not actually sure of - a null here is fine, a wrong answer is not.
If these tracks are clearly by many different original artists - a hits
compilation - return {"artist": "Various Artists", "album": <the collection's
name if it is well known, else null>}.
"""


def _guess_release_from_tracks(titles, model=None, timeout=45):
    """Ask the model which record a bare tracklist is from. A hypothesis, not a
    fact - the caller runs it past Discogs/MusicBrainz before trusting it."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None

    listing = "\n".join("- " + t for t in titles[:20])
    r = requests.post(
        API,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json", "X-Title": "Slipmat"},
        json={"model": model or os.environ.get("VINYL_MODEL") or DEFAULT_MODEL,
              "temperature": 0,
              "messages": [{"role": "user", "content": GUESS_PROMPT % listing}]},
        timeout=timeout,
    )
    body = r.json()
    if r.status_code != 200 or "choices" not in body:
        return None
    text = body["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        g = json.loads(text)
    except json.JSONDecodeError:
        return None
    return g if isinstance(g, dict) else None


_VARIOUS = {"various artists", "various", "va", "v/a", "diverse artister",
            "olika artister"}


def _backfill_track_details(tracks, source_tracks):
    """Fill blank durations and per-track artists by matching track titles
    against a confirmed database release. Positions and titles came off the
    sleeve and are never touched here - only what's missing beside them.
    Per-track artist mainly matters for a various-artists compilation, where
    each song has a different original performer; a database's own data for
    that isn't always complete, so this only ever adds, never overwrites.
    Returns (durations_filled, artists_filled).
    """
    import identify

    by_title = {identify._norm(t.get("title")): t
               for t in (source_tracks or []) if t.get("title")}
    durations = artists = 0
    for t in tracks:
        if not t.get("title"):
            continue
        match = by_title.get(identify._norm(t["title"]))
        if not match:
            continue
        if not t.get("duration") and match.get("duration"):
            t["duration"] = match["duration"]
            durations += 1
        if not t.get("artist") and match.get("artist"):
            t["artist"] = match["artist"]
            artists += 1
    return durations, artists


def _backfill_artists_from_musicbrainz(tracks, artist, album, catalog_number=None):
    """A various-artists compilation whose Discogs tracklist came back with
    no artist on any track - Discogs' contributors never annotated this one,
    which happens for the less-documented compilations. MusicBrainz
    sometimes has the credits Discogs doesn't. Only ever fills an artist
    name, matched by title; never adds, removes, or reorders a track, and
    only runs when every track is missing one - a partial Discogs credit
    list is left alone rather than mixed with a different source.
    """
    import identify

    if not tracks or any(t.get("artist") for t in tracks):
        return 0

    by_title = identify.mb_track_artists(artist, album, catalog_number)
    filled = 0
    for t in tracks:
        got = by_title.get(identify._norm(t.get("title")))
        if got:
            t["artist"] = got
            filled += 1
    return filled


def enrich_from_release(rec, images=None, model=None):
    """Consult the databases once the release is identified, and prefer what
    they hold over what the photo could make out.

    Discogs catalogues the individual pressing - side positions, per-track
    timings, and the songs inside a medley, which a sleeve usually prints as
    one line - so when it confirms the release its tracklist replaces the
    sleeve read outright, and tracklist_source says so. The sleeve is what
    identifies the record and remains the fallback: if Discogs has no
    confident match, or its tracklist comes back empty, the photo's own
    reading stands untouched.

    Year, label, and catalogue number only ever fill a blank, from whichever
    source matched, and never overwrite what was legible on the sleeve. The
    cover is different: text agreement on artist/album/catalogue number
    still leaves several real pressings tied, each with its own artwork, so
    it gets compared against the actual photographed sleeve - see
    _resolve_cover - rather than just taking the top-ranked match's.
    """
    import identify

    artist, album = rec.get("artist"), rec.get("album")
    if not artist or not album:
        return rec

    tracks = rec.get("tracks") or []
    try:
        hits = identify.verify_text(artist, album, rec.get("catalog_number"))
    except identify.DatabaseUnavailable:
        return rec
    if not hits or hits[0]["match"] == "weak":
        return rec

    best = hits[0]
    try:
        full = identify.resolve_release(best)
    except identify.DatabaseUnavailable:
        return rec

    if not rec.get("year"):
        rec["year"] = full.get("year")
    if not rec.get("label"):
        rec["label"] = full.get("label")
    if not rec.get("catalog_number"):
        rec["catalog_number"] = full.get("catalog_number")
    _resolve_cover(rec, hits, images, model)

    # Track-level data only ever comes from Discogs - a MusicBrainz release
    # blends pressings and formats in a way that isn't trustworthy per track,
    # only for the release-wide fields above.
    discogs = [h for h in hits if h["source"] == "discogs"]
    filled = ""
    if discogs and discogs[0]["match"] != "weak":
        if discogs[0] is not best:
            best = discogs[0]           # hits[0] was MusicBrainz; re-fetch
            try:
                full = identify.resolve_release(best)
            except identify.DatabaseUnavailable:
                full = {}

        mb_artists = 0
        if full.get("tracks"):
            rec["tracks"] = full["tracks"]
            rec["tracklist_source"] = "discogs (%s %s)" % (
                best["format"], best.get("date") or "")
            # Discogs gave the tracklist but sometimes nobody's annotated who
            # performs which song - a gap in Discogs' own data, not this
            # app's. MusicBrainz occasionally has just that, so it's worth
            # one more request, but only for a various-artists release: an
            # ordinary album's tracks correctly have no artist of their own.
            if artist.strip().lower() in _VARIOUS:
                mb_artists = _backfill_artists_from_musicbrainz(
                    rec["tracks"], artist, album, rec.get("catalog_number"))
        elif tracks:
            # Nothing to replace it with - keep the sleeve's, top up its gaps.
            durations, artists = _backfill_track_details(tracks, full.get("tracks"))
            bits = ["%d duration%s" % (durations, "" if durations == 1 else "s")] if durations else []
            if artists:
                bits.append("%d track artist%s" % (artists, "" if artists == 1 else "s"))
            filled = ", %s filled" % ", ".join(bits) if bits else ""

        if mb_artists:
            filled += ", %d artist%s from musicbrainz" % (
                mb_artists, "" if mb_artists == 1 else "s")

    rec["details_source"] = "%s (%s%s)" % (best["source"], best["match"], filled)
    return rec


def name_from_tracklist(rec, images=None, model=None):
    """The sleeve gave a tracklist but no album (or no artist). Ask the model
    which record these songs are from, then confirm that guess against
    Discogs/MusicBrainz - the same verifier the metadata path already uses -
    before writing anything. The tracklist itself is never touched; it came
    off the sleeve and is trusted over any database. Mutates and returns rec;
    sets rec["release_source"] when it fills anything.
    """
    import identify

    titles = [t.get("title") for t in (rec.get("tracks") or []) if t.get("title")]
    if len(titles) < 2:
        return rec

    guess = _guess_release_from_tracks(titles, model)
    if not guess:
        return rec

    # A hits compilation can't be pinned from its tracklist - a dozen of them
    # carry the same singles, and the model will happily name the wrong one.
    # Take "Various Artists" for the missing artist and stop there; a sleeve
    # album title already beats a guess, and a guessed one is worse than blank.
    if (guess.get("artist") or "").strip().lower() in _VARIOUS:
        if not rec.get("artist"):
            rec["artist"] = "Various Artists"
            rec["release_source"] = "recognised as a various-artists compilation"
        return rec

    if not guess.get("album"):
        return rec

    hits = identify.verify_text(guess.get("artist"), guess.get("album"))
    if not hits or hits[0]["match"] == "weak":
        return rec

    best = hits[0]
    try:
        full = identify.resolve_release(best)
    except identify.DatabaseUnavailable:
        full = {}

    rec["artist"] = rec.get("artist") or best.get("artist") or guess.get("artist")
    rec["album"] = rec.get("album") or best.get("title") or guess.get("album")
    if not rec.get("year"):
        rec["year"] = full.get("year") or best.get("date")
    if not rec.get("label"):
        rec["label"] = full.get("label")
    if not rec.get("catalog_number"):
        rec["catalog_number"] = full.get("catalog_number") or best.get("catno")
    _resolve_cover(rec, hits, images, model)
    # Per-track duration/artist only ever comes from Discogs, same reasoning
    # as fill_tracklist - a MusicBrainz release blends data across pressings
    # in a way that isn't trustworthy at the individual-track level.
    if best["source"] == "discogs":
        _backfill_track_details(rec.get("tracks") or [], full.get("tracks"))
    rec["release_source"] = "identified from the tracklist (%s, %s)" % (
        best["match"], best["source"])
    return rec


def scan_and_fill(images, **kw):
    """Full pipeline: read the sleeve, fall back to the database if needed.

    The sleeve read is the valuable part. If Discogs and MusicBrainz are both
    throttling or down, that must never discard a good identification -
    degrade to "no tracklist" and say why.
    """
    import identify

    rec = scan_images(images, **kw)
    rec["tracklist_source"] = "sleeve"

    model = kw.get("model")
    if not rec.get("tracks"):
        try:
            rec, best = fill_tracklist(rec, images=images, model=model)
            rec["tracklist_source"] = (
                "%s (%s %s)" % (best["source"], best["format"], best.get("date") or "")
                if best else "none"
            )
        except identify.DatabaseUnavailable as exc:
            rec["tracks"] = []
            rec["tracklist_source"] = "unavailable"
            rec["tracklist_error"] = (
                "The release database is temporarily unavailable (%s). The "
                "record was identified fine - photograph the back cover to "
                "read the tracklist directly, or retry in a minute." % exc)

    else:
        # The sleeve gave a tracklist. If it didn't name the release, work
        # backwards from the songs first, so there's something to search on.
        if not rec.get("album") or not rec.get("artist"):
            try:
                rec = name_from_tracklist(rec, images=images, model=model)
            except identify.DatabaseUnavailable:
                pass   # a nameless tracklist is still useful

        # Then look the release up regardless. Discogs catalogues the exact
        # pressing, so its tracklist supersedes the photo's reading of one -
        # see enrich_from_release. The cover comparison is the one place
        # this now costs a second vision call beyond the request itself -
        # only when more than one candidate cover exists to tell apart.
        try:
            rec = enrich_from_release(rec, images=images, model=model)
        except identify.DatabaseUnavailable:
            pass   # keep the sleeve read; the blanks stay blank

    return rec


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("usage: python vinyl.py front.jpg [back.jpg ...] [--json]")

    record = scan_and_fill([open(p, "rb").read() for p in args])

    if "--json" in sys.argv:
        print(json.dumps(record, indent=2))
    else:
        print("\n  %s\n  %s" % (record.get("artist") or "?",
                                record.get("album") or "?"))
        meta = [x for x in (record.get("year"), record.get("label"),
                            record.get("catalog_number")) if x]
        if meta:
            print("  " + "  ".join(meta))
        print()
        for t in record.get("tracks") or []:
            print("   %-4s %-42s %s" % (t.get("position") or "",
                                        t.get("title", "?"),
                                        t.get("duration") or ""))
        print("\n  confidence: %s   tracklist: %s"
              % (record.get("confidence"), record.get("tracklist_source")))
        if record.get("release_source"):
            print("  release: %s" % record["release_source"])
