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

import requests

API = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-2.5-flash"


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


def fill_tracklist(rec):
    """Only used when the photos showed no tracklist. Free, needs no
    OpenRouter key.

    The tracklist itself only ever comes from Discogs. It's built around the
    exact vinyl pressing - A1/B1 side positions, per-format track order -
    where MusicBrainz's release-group model blends data across pressings and
    formats loosely enough that its tracklist isn't reliable for "where to
    drop the needle", the whole reason this app exists. Metadata (year,
    label, catalogue number, cover) still uses whichever source matched,
    Discogs preferred - a wrong pressing's year or label is a small miss,
    a wrong pressing's tracklist actively sends you to the wrong track.
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
    if full.get("cover_url"):
        rec["cover_url"] = full["cover_url"]

    discogs_hits = [h for h in hits if h["source"] == "discogs"]
    if not discogs_hits or discogs_hits[0]["match"] == "weak":
        return rec, None   # metadata confirmed, but no Discogs tracklist to use

    best = discogs_hits[0]
    if best is not hits[0]:
        full = identify.resolve_release(best)   # re-fetch: hits[0] was MusicBrainz
    rec["tracks"] = full["tracks"]
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


def enrich_from_release(rec):
    """Consult the databases once the release is identified, and prefer what
    they hold over what the photo could make out.

    Discogs catalogues the individual pressing - side positions, per-track
    timings, and the songs inside a medley, which a sleeve usually prints as
    one line - so when it confirms the release its tracklist replaces the
    sleeve read outright, and tracklist_source says so. The sleeve is what
    identifies the record and remains the fallback: if Discogs has no
    confident match, or its tracklist comes back empty, the photo's own
    reading stands untouched.

    Everything else (year, label, catalogue number, cover) only ever fills a
    blank, from whichever source matched, and never overwrites what was
    legible on the sleeve.
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
    if not rec.get("cover_url") and full.get("cover_url"):
        rec["cover_url"] = full["cover_url"]

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

        if full.get("tracks"):
            rec["tracks"] = full["tracks"]
            rec["tracklist_source"] = "discogs (%s %s)" % (
                best["format"], best.get("date") or "")
        elif tracks:
            # Nothing to replace it with - keep the sleeve's, top up its gaps.
            durations, artists = _backfill_track_details(tracks, full.get("tracks"))
            bits = ["%d duration%s" % (durations, "" if durations == 1 else "s")] if durations else []
            if artists:
                bits.append("%d track artist%s" % (artists, "" if artists == 1 else "s"))
            filled = ", %s filled" % ", ".join(bits) if bits else ""

    rec["details_source"] = "%s (%s%s)" % (best["source"], best["match"], filled)
    return rec


def name_from_tracklist(rec, model=None):
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
    if not rec.get("cover_url") and full.get("cover_url"):
        rec["cover_url"] = full["cover_url"]
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

    if not rec.get("tracks"):
        try:
            rec, best = fill_tracklist(rec)
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
                rec = name_from_tracklist(rec, model=kw.get("model"))
            except identify.DatabaseUnavailable:
                pass   # a nameless tracklist is still useful

        # Then look the release up regardless. Discogs catalogues the exact
        # pressing, so its tracklist supersedes the photo's reading of one -
        # see enrich_from_release. Costs nothing beyond the request itself:
        # no vision call, no extra photo.
        try:
            rec = enrich_from_release(rec)
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
