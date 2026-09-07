"""
Core: photographs of a record in, structured release info out.

Vision runs through OpenRouter so the model is a one-line config change.
The tracklist is read straight off the back sleeve; MusicBrainz is consulted
only when the photos do not show one - or, when the sleeve names no album, to
confirm the release the model identifies from the tracklist.
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
 "tracks": [{"position": str|null, "title": str, "duration": str|null}],
 "confidence": "high"|"medium"|"low",
 "notes": str}

"position" is the side-and-index marking printed beside each track: "A1", "A2",
"B1" and so on. Preserve the printed order. If sides are shown but tracks are
not individually numbered, number them yourself in printed order.

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


def _tracklist_from_release(data):
    """A MusicBrainz release payload (inc=recordings) -> our track dicts."""
    tracks = []
    for medium in data.get("media", []):
        for t in medium.get("tracks", []):
            ms = t.get("length")
            tracks.append({
                "position": t.get("number"),
                "title": t.get("title"),
                "duration": ("%d:%02d" % (ms // 60000, ms // 1000 % 60)) if ms else None,
            })
    return tracks


def _label_name(release_data):
    for li in (release_data or {}).get("label-info") or []:
        name = (li.get("label") or {}).get("name")
        if name:
            return name
    return None


def fill_tracklist(rec):
    """Only used when the photos showed no tracklist. Free, needs no key."""
    import identify

    hits = identify.verify_text(rec.get("artist"), rec.get("album"),
                                rec.get("catalog_number"))
    if not hits and rec.get("catalog_number"):
        hits = identify.verify_text(rec.get("artist"), rec.get("album"))
    if not hits or hits[0]["match"] == "weak":
        return rec, None

    best = hits[0]
    data = identify._mb_get("release/" + best["mbid"], inc="recordings")
    rec["tracks"] = _tracklist_from_release(data)
    if not rec.get("year"):
        rec["year"] = best.get("date")
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
"""


def _guess_release_from_tracks(titles, model=None, timeout=45):
    """Ask the model which record a bare tracklist is from. A hypothesis, not a
    fact - the caller runs it past MusicBrainz before trusting it."""
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


def name_from_tracklist(rec, model=None):
    """The sleeve gave a tracklist but no album title. Ask the model which
    record these songs are from, then confirm that guess against MusicBrainz -
    the same verifier the metadata path already uses - before writing anything.
    The tracklist itself is never touched; it came off the sleeve and is
    trusted over any database. Returns (rec, best_match_or_None).
    """
    import identify

    titles = [t.get("title") for t in (rec.get("tracks") or []) if t.get("title")]
    if len(titles) < 2:
        return rec, None

    guess = _guess_release_from_tracks(titles, model)
    if not guess or not guess.get("album"):
        return rec, None

    hits = identify.verify_text(guess.get("artist"), guess.get("album"))
    if not hits or hits[0]["match"] == "weak":
        return rec, None

    best = hits[0]
    try:
        full = identify._mb_get("release/" + best["mbid"],
                                attempts=1, inc="labels artist-credits")
    except identify.MusicBrainzUnavailable:
        full = {}

    rec["artist"] = rec.get("artist") or best.get("artist") or guess.get("artist")
    rec["album"] = rec.get("album") or best.get("title") or guess.get("album")
    if not rec.get("year"):
        rec["year"] = best.get("date")
    if not rec.get("label"):
        rec["label"] = _label_name(full)
    if not rec.get("catalog_number"):
        rec["catalog_number"] = best.get("catno") or ", ".join(
            li.get("catalog-number", "") for li in (full.get("label-info") or [])
            if li.get("catalog-number")) or None
    return rec, best


def scan_and_fill(images, **kw):
    """Full pipeline: read the sleeve, fall back to the database if needed.

    The sleeve read is the valuable part. If MusicBrainz is throttling or down,
    that must never discard a good identification - degrade to "no tracklist"
    and say why.
    """
    import identify

    rec = scan_images(images, **kw)
    rec["tracklist_source"] = "sleeve"

    if not rec.get("tracks"):
        try:
            rec, best = fill_tracklist(rec)
            rec["tracklist_source"] = (
                "musicbrainz (%s %s)" % (best["format"], best.get("date") or "")
                if best else "none"
            )
        except identify.MusicBrainzUnavailable as exc:
            rec["tracks"] = []
            rec["tracklist_source"] = "unavailable"
            rec["tracklist_error"] = (
                "MusicBrainz is temporarily unavailable (%s). The record was "
                "identified fine - photograph the back cover to read the "
                "tracklist directly, or retry in a minute." % exc)

    elif not rec.get("album") or not rec.get("artist"):
        # The tracklist read fine but the sleeve never names the release - a
        # white label, or a cover that's all artwork. Work backwards from the
        # songs. tracklist_source stays "sleeve": the tracks are still the
        # sleeve's, only the release name is looked up.
        try:
            rec, best = name_from_tracklist(rec, model=kw.get("model"))
            if best:
                rec["release_source"] = "identified from the tracklist (%s)" % best["match"]
        except identify.MusicBrainzUnavailable:
            pass   # keep the sleeve read; a nameless tracklist is still useful

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
