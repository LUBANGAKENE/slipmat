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

class MusicBrainzUnavailable(RuntimeError):
    """MusicBrainz is throttling or down. Transient - not a failed lookup."""


def _mb_get(path, attempts=3, **params):
    """MusicBrainz is 1 req/sec and wants a real User-Agent. Be a good citizen.

    503 from MusicBrainz means throttled or overloaded, not "no such release",
    so it is retried with backoff and then raised as its own error type. The
    caller can then degrade gracefully instead of failing the whole scan.
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

        if r.status_code in (429, 503):
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
        "mbid": r.get("id"),
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


def verify_text(artist, album, catalog_number=None):
    """Turn a model hypothesis into confirmed releases, best match first.

    Two passes. Strict phrases first, because when they hit they are right.
    Then a fuzzy pass, because vision and OCR misread characters constantly
    and an exact-match-only verifier throws away every near miss.
    """
    strict = []
    if album:
        strict.append('release:"%s"' % album)
    if artist:
        strict.append('artist:"%s"' % artist)
    if catalog_number:
        strict.append('catno:"%s"' % catalog_number)
    if not strict:
        return []

    data = _mb_get("release", query=" AND ".join(strict), limit=10)
    releases = data.get("releases", [])

    if not releases:
        loose = [q for q in (_fuzzy("release", album),
                             _fuzzy("artist", artist)) if q]
        if loose:
            data = _mb_get("release", query=" AND ".join(loose), limit=10)
            releases = data.get("releases", [])

    out = [_release(r) for r in releases]

    # MusicBrainz's own score is lenient; re-rank on how well the strings agree.
    want_a, want_t = _norm(artist), _norm(album)
    for rel in out:
        got_a, got_t = _norm(rel["artist"]), _norm(rel["title"])
        exact = (got_a == want_a) + (got_t == want_t)
        loose = ((want_a in got_a or got_a in want_a) +
                 (want_t in got_t or got_t in want_t))
        rel["match"] = ("exact" if exact == 2
                        else "strong" if exact + loose >= 2
                        else "weak")

    # Prefer vinyl pressings. A CD release numbers its tracks 1,2,3 - only the
    # vinyl carries the A1/B1 side positions that tell you where to drop the
    # needle, which is the whole reason we're looking this up.
    order = {"exact": 0, "strong": 1, "weak": 2}
    return sorted(out, key=lambda r: (order[r["match"]],
                                      0 if "vinyl" in (r["format"] or "").lower() else 1,
                                      -r["score"]))


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
        print("             mbid %s" % c["mbid"])
