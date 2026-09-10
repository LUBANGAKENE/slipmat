"""
Web dashboard for Slipmat.

    python app.py

/ is the homepage; the scan tool itself is at /app. Binds on all interfaces,
so you can open the tool on your phone over the same wifi and shoot the
sleeve directly with the camera - which is how you'd actually use this next
to the decks.
"""

import json
import os
import re
import socket
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, render_template, request

import bpm
import identify
import vinyl

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024   # a few phone photos


@app.get("/")
def home():
    return render_template("home.html")


@app.get("/app")
def index():
    # Both are safe to hand to the browser: the anon key is public by design,
    # protection comes from the Row Level Security policies in
    # supabase/schema.sql, not from keeping this secret the way the API keys
    # in .env are. Blank strings mean "no library configured" - the page
    # notices and hides that part of the UI rather than erroring.
    return render_template("index.html",
                           supabase_url=os.environ.get("SUPABASE_URL", ""),
                           supabase_anon_key=os.environ.get("SUPABASE_ANON_KEY", ""))


@app.get("/brand")
def brand():
    return render_template("brand.html")


@app.post("/api/scan")
def api_scan():
    payload = request.get_json(silent=True) or {}
    images = payload.get("images") or []
    if not images:
        return jsonify({"error": "no images supplied"}), 400
    if len(images) > 4:
        return jsonify({"error": "at most 4 photos per scan"}), 400

    try:
        record = vinyl.scan_and_fill(images, model=payload.get("model") or None)
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 502

    usage = record.get("_usage") or {}
    record["_cost"] = usage.get("cost")
    return jsonify(record)


@app.post("/api/bpm")
def api_bpm():
    """Second pass, once the tracklist is already on screen.

    A ten-track LP is ten lookups, and the sleeve read is what you are actually
    waiting for - so this runs as its own request and the column fills in after.
    One track failing must not cost you the other nine, hence the per-track try.
    """
    payload = request.get_json(silent=True) or {}
    artist = payload.get("artist")
    titles = payload.get("titles") or []
    # Per-track artist, for a various-artists compilation where every song
    # has a different original performer - "Various Artists" as the search
    # artist finds nothing useful. Falls back to the release-level artist
    # when a track has none of its own, i.e. every ordinary release.
    track_artists = payload.get("artists") or []
    if not titles:
        return jsonify({"error": "no titles supplied"}), 400
    # Headroom over a long LP's track count, because the songs inside a
    # megamix are looked up alongside the tracks holding them.
    if len(titles) > 60:
        return jsonify({"error": "at most 60 tracks per request"}), 400

    # "Various Artists" (or "VA", "Unknown Artist", ...) is worse than no
    # artist at all as a search term - it constrains the lookup to a name
    # that doesn't exist in Spotify's or ReccoBeats' catalogue, rather than
    # leaving the field open to a title-only search. Only a real
    # release-level artist name is worth falling back to.
    fallback_artist = None if identify.is_generic_artist(artist) else artist

    def one(args):
        title, track_artist = args
        try:
            feat = bpm.lookup(track_artist or fallback_artist, title)
        except Exception:
            traceback.print_exc()
            return None
        if not feat:
            return None
        # Which recording the numbers came from. matched_artist is newer than
        # the cache file, so fall back to splitting the "Artist - Title"
        # string rows written before it existed still carry.
        matched = feat.get("matched")
        return {"bpm": feat["bpm"],
                "key": feat.get("camelot") or feat.get("key"),
                "source": feat["source"],
                "matched": matched,
                "matched_artist": (feat.get("matched_artist")
                                   or (matched.split(" - ")[0] if matched else None)),
                "spotify_id": feat.get("spotify_id")}

    pairs = [(t, track_artists[i] if i < len(track_artists) else None)
            for i, t in enumerate(titles)]

    # Run them side by side. Serially, a ten-track LP left the column showing
    # dots for the better part of half a minute; the lookup client keeps its
    # own rate limit, so widening this further just queues up inside it.
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one, pairs))

    return jsonify({"results": results})


@app.post("/api/track-artists")
def api_track_artists():
    """Third pass, fired alongside BPM lookup once the tracklist is already
    on screen: MusicBrainz search per track title, for the rare track still
    unlabelled after both an album-level Discogs lookup and an album-level
    MusicBrainz lookup found nothing - a record obscure enough that neither
    service has it catalogued as a release at all. See
    identify.recording_artist for why this is conservative about answering
    at all: title search alone can't tell two different songs called the
    same thing apart, so ambiguous ones come back null rather than guessed.

    Workers=1, not the 6 BPM uses: MusicBrainz enforces one request per
    second server-side regardless, and _mb_get's throttle is a single
    process-wide timestamp, not a per-thread one - running these
    concurrently would just have threads contending over it for no gain.
    """
    payload = request.get_json(silent=True) or {}
    titles = payload.get("titles") or []
    before_year = payload.get("year")
    if not titles:
        return jsonify({"error": "no titles supplied"}), 400
    if len(titles) > 40:
        return jsonify({"error": "at most 40 tracks per request"}), 400

    def one(title):
        try:
            return identify.recording_artist(title, before_year)
        except Exception:
            traceback.print_exc()
            return None

    results = [one(t) for t in titles]
    return jsonify({"results": results})


# --------------------------------------------------------------- track preview

# Spotify id -> (preview url or None, fetched_at). A None is cached too, with a
# shorter life, so a track that genuinely has no preview isn't refetched on
# every click - but retried within the hour in case one appears.
_PREVIEW = {}
_PREVIEW_LOCK = threading.Lock()
_PREVIEW_HIT_TTL = 24 * 3600
_PREVIEW_MISS_TTL = 3600
_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_PREVIEW_IN_PAGE = re.compile(r'"audioPreview"\s*:\s*\{\s*"url"\s*:\s*"([^"]+)"')


@app.get("/api/preview/<track_id>")
def api_preview(track_id):
    """A 30-second preview URL for a Spotify track id, for the little play
    button on a tracklist row.

    Spotify stopped returning preview_url from its API for apps registered
    after late 2024, but its embed player still carries one. The browser
    can't read that page itself (cross-origin, no CORS header), so we fetch
    it here and hand back just the mp3 URL - which p.scdn.co *does* serve to
    an <audio> element from any origin. Cached per process so replaying a row
    is instant.
    """
    if not _SPOTIFY_ID.match(track_id or ""):
        return jsonify({"error": "bad track id"}), 400

    now = time.time()
    with _PREVIEW_LOCK:
        cached = _PREVIEW.get(track_id)
    if cached:
        url, at = cached
        ttl = _PREVIEW_HIT_TTL if url else _PREVIEW_MISS_TTL
        if now - at < ttl:
            return (jsonify({"url": url}) if url
                    else (jsonify({"error": "no preview for this track"}), 404))

    url = None
    try:
        r = requests.get("https://open.spotify.com/embed/track/" + track_id,
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.ok:
            m = _PREVIEW_IN_PAGE.search(r.text)
            if m:
                # The capture is a JSON string body (escaped "\/", "-", …);
                # decode it as one rather than hand the browser a mangled URL.
                url = json.loads('"%s"' % m.group(1))
    except requests.RequestException:
        traceback.print_exc()
        # A network blip is not "no preview" - don't poison the cache with it.
        return jsonify({"error": "could not reach Spotify"}), 502

    with _PREVIEW_LOCK:
        _PREVIEW[track_id] = (url, now)
    return (jsonify({"url": url}) if url
            else (jsonify({"error": "no preview for this track"}), 404))


def lan_ip():
    """Best-effort LAN address so the phone can find this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    ip = lan_ip()
    print("\n  slipmat")
    print("    this machine : http://127.0.0.1:5000/app")
    print("    your phone   : http://%s:5000/app\n" % ip)
    app.run(host="0.0.0.0", port=5000, debug=False)
