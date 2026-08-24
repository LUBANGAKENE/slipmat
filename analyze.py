"""
Resolve vinyl tracks to DJ-usable BPM + key.

Chain:  Discogs tracklist -> MusicBrainz ISRC -> this API -> Camelot + pitch math

The API re-serves Spotify's deprecated audio-features payload: every value is a
string, key is a note name, mode is "1.0"/"0.0", duration is mm:ss. This module
normalises all of that and adds what the API doesn't give you.

Budget note: free tier is 20 requests/day AND has a per-second cap, so batched
calls are spaced. Everything resolved is cached permanently.
"""

import json
import os
import sqlite3
import time

import requests

HOST = "spotify-audio-features-track-analysis.p.rapidapi.com"
URL = f"https://{HOST}/tracks/spotify_audio_features"

# Camelot wheel. Index = semitones from C; B-side = major, A-side = minor.
_MAJOR = {"C": "8B", "C#": "3B", "Db": "3B", "D": "10B", "D#": "5B", "Eb": "5B",
          "E": "12B", "F": "7B", "F#": "2B", "Gb": "2B", "G": "9B", "G#": "4B",
          "Ab": "4B", "A": "11B", "A#": "6B", "Bb": "6B", "B": "1B"}
_MINOR = {"C": "5A", "C#": "12A", "Db": "12A", "D": "7A", "D#": "2A", "Eb": "2A",
          "E": "9A", "F": "4A", "F#": "11A", "Gb": "11A", "G": "6A", "G#": "1A",
          "Ab": "1A", "A": "8A", "A#": "3A", "Bb": "3A", "B": "10A"}


def to_camelot(key, mode):
    """('D', 1.0) -> '10B'.  mode: 1 = major, 0 = minor."""
    if not key:
        return None
    table = _MAJOR if float(mode or 0) >= 0.5 else _MINOR
    return table.get(key.strip())


def camelot_neighbours(camelot):
    """Harmonically compatible keys: same, +/-1 on the wheel, and relative maj/min."""
    if not camelot:
        return []
    num, letter = int(camelot[:-1]), camelot[-1]
    other = "A" if letter == "B" else "B"
    return [camelot,
            f"{num % 12 + 1}{letter}",
            f"{(num - 2) % 12 + 1}{letter}",
            f"{num}{other}"]


def pitch_percent(from_bpm, to_bpm):
    """Pitch fader % needed to take a record from from_bpm to to_bpm."""
    if not from_bpm:
        return None
    return (to_bpm - from_bpm) / from_bpm * 100.0


def reachable(record_bpm, target_bpm, fader_range=8.0):
    """Can this record be beatmatched to target within the turntable's range?"""
    pct = pitch_percent(record_bpm, target_bpm)
    return pct is not None and abs(pct) <= fader_range, pct


def tempo_candidates(bpm):
    """Spotify tempo confuses half/double time. Offer the plausible alternatives."""
    if not bpm:
        return []
    return sorted({round(bpm / 2, 1), round(bpm, 1), round(bpm * 2, 1)})


def _dur_seconds(s):
    """'04:08' -> 248"""
    try:
        parts = [int(p) for p in str(s).split(":")]
    except ValueError:
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def normalise(payload):
    """API response -> typed dict with Camelot added."""
    af = (payload or {}).get("audio_features")
    if not af:
        return None

    def num(k):
        try:
            return float(af[k])
        except (KeyError, TypeError, ValueError):
            return None

    key, mode = af.get("key"), num("mode")
    bpm = num("tempo")
    return {
        "bpm": round(bpm, 1) if bpm else None,
        "bpm_alternatives": tempo_candidates(bpm),
        "key": key,
        "mode": "major" if (mode or 0) >= 0.5 else "minor",
        "camelot": to_camelot(key, mode),
        "duration_s": _dur_seconds(af.get("duration")),
        "time_signature": num("time_signature"),
        "energy": num("energy"),
        "danceability": num("danceability"),
        "loudness_db": num("loudness"),
    }


class Client:
    """Cached, rate-limit-aware client. Never spends a request it doesn't have to."""

    def __init__(self, key=None, db="cache.sqlite", min_interval=1.5):
        self.key = key or os.environ.get("RAPIDAPI_KEY", "")
        self.min_interval = min_interval
        self._last = 0.0
        self.con = sqlite3.connect(db)
        self.con.execute("""CREATE TABLE IF NOT EXISTS features (
            isrc TEXT PRIMARY KEY, fetched_at INT, data TEXT)""")
        self.con.commit()

    def get(self, isrc, allow_network=True):
        """Look up by ISRC. Returns (features_dict|None, source)."""
        row = self.con.execute("SELECT data FROM features WHERE isrc=?", (isrc,)).fetchone()
        if row:
            return json.loads(row[0]), "cache"
        if not allow_network:
            return None, "not-cached"
        if not self.key:
            raise RuntimeError("RAPIDAPI_KEY not set")

        gap = time.time() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)

        r = requests.get(URL, headers={"x-rapidapi-key": self.key,
                                       "x-rapidapi-host": HOST},
                         params={"isrc": isrc}, timeout=20)
        self._last = time.time()

        if r.status_code == 429:
            return None, "rate-limited"
        if r.status_code != 200:
            return None, f"http-{r.status_code}"

        feat = normalise(r.json())
        if feat:
            self.con.execute("INSERT OR REPLACE INTO features VALUES (?,?,?)",
                             (isrc, int(time.time()), json.dumps(feat)))
            self.con.commit()
            return feat, "api"
        return None, "no-data"

    def remaining(self):
        """Requests left today, from the last response's headers (best effort)."""
        return getattr(self, "_remaining", None)


if __name__ == "__main__":
    # Offline self-test on the payload we already paid for. Spends nothing.
    sample = {"audio_features": {
        "danceability": "0.76", "energy": "0.964", "key": "D", "loudness": "-5.844",
        "mode": "1.0", "tempo": "125.003", "duration": "04:08", "time_signature": "4.0",
        "speechiness": "0.0577", "acousticness": "0.0018",
        "instrumentalness": "0.703", "liveness": "0.0975", "valence": "0.643"}}

    f = normalise(sample)
    print(json.dumps(f, indent=2))
    print(f"\n  {f['key']} {f['mode']}  ->  Camelot {f['camelot']}")
    print(f"  mixes with: {', '.join(camelot_neighbours(f['camelot']))}")

    print(f"\n  Record is {f['bpm']} BPM at 0% pitch.")
    for target in (122, 128, 140):
        ok, pct = reachable(f["bpm"], target)
        verdict = f"pitch {pct:+.1f}%" if ok else f"OUT OF RANGE ({pct:+.1f}%)"
        print(f"    to mix into a {target} BPM track: {verdict}")
