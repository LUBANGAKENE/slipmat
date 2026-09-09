"""
Photograph a record, get back what it is and what's on it.

    python scan.py front.jpg
    python scan.py front.jpg back.jpg      <- back cover carries the tracklist

The tracklist is printed on the sleeve, so the model reads it directly. The
database is only consulted when the photo does not show one (front only, or
the back is too worn to read).

Setup:
    pip install requests
    $env:GEMINI_API_KEY="..."      free, no card, from aistudio.google.com
"""

import json
import os
import re
import sys

# Windows consoles default to cp1252 and mangle the curly apostrophes and
# accents that turn up in half of all track titles.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

PROMPT = """These photographs show a vinyl record - its sleeve, back cover,
and/or centre label.

Read the printed text and report what this release is. Read what is actually
there; do not infer from artwork style or complete titles from memory. If
something is not legible, leave it null rather than guessing.

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
"B1" and so on. Keep the printed order. If the sleeve lists sides without
numbering each track, number them yourself in printed order (A1, A2, ...).

"duration" only if a running time is printed next to the track.

If no tracklist is visible in any photo, return "tracks": []."""


def scan(paths, api_key=None, model="gemini-2.5-flash"):
    """Send every photo in one request so the model can cross-reference them."""
    import google.generativeai as genai
    from PIL import Image

    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY not set - get one free at aistudio.google.com")
    genai.configure(api_key=key)

    images = []
    for p in paths:
        if not os.path.exists(p):
            sys.exit("no such file: " + p)
        img = Image.open(p)
        img.thumbnail((1568, 1568))   # keep small print legible, stay cheap
        images.append(img)

    resp = genai.GenerativeModel(model).generate_content([PROMPT] + images)
    text = (resp.text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        sys.exit("model returned unparseable output:\n" + text[:500])


def fill_tracklist(rec):
    """Only used when the photos showed no tracklist. Free, no key."""
    import identify

    hits = identify.verify_text(rec.get("artist"), rec.get("album"),
                                rec.get("catalog_number"))
    if not hits and rec.get("catalog_number"):
        hits = identify.verify_text(rec.get("artist"), rec.get("album"))
    if not hits or hits[0]["match"] == "weak":
        return rec, None

    best = hits[0]
    full = identify.resolve_release(best)
    rec["tracks"] = full["tracks"]
    rec.setdefault("year", full.get("year"))
    return rec, best


def show(rec, source=None):
    artist = rec.get("artist") or "Unknown artist"
    album = rec.get("album") or "Unknown album"
    print("\n  %s\n  %s" % (artist, album))

    meta = [x for x in (rec.get("year"), rec.get("label"),
                        rec.get("catalog_number")) if x]
    if meta:
        print("  " + "  ".join(meta))

    tracks = rec.get("tracks") or []
    if tracks:
        print()
        for t in tracks:
            pos = (t.get("position") or "").ljust(4)
            dur = t.get("duration") or ""
            print("   %s %-42s %s" % (pos, t.get("title", "?"), dur))
    else:
        print("\n   no tracklist found - photograph the back cover")

    tail = "  confidence: %s" % rec.get("confidence", "?")
    if source:
        tail += "   tracklist from %s" % source
    print("\n" + tail)
    if rec.get("notes"):
        print("  " + rec["notes"])


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("usage: python scan.py front.jpg [back.jpg ...] [--json]")

    record = scan(args)

    source = None
    if not record.get("tracks"):
        record, best = fill_tracklist(record)
        if best:
            source = "%s (%s %s)" % (best["source"], best["format"], best.get("date") or "")

    if "--json" in sys.argv:
        print(json.dumps(record, indent=2))
    else:
        show(record, source)
