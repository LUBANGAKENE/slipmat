"""
Coverage probe for the SoundNet track-analysis API (RapidAPI).

Answers one question: what fraction of YOUR records does this thing actually know?
Run it against a real crate, not a curated list.

Usage:
    set RAPIDAPI_KEY=your_key_here          (PowerShell: $env:RAPIDAPI_KEY="...")
    python probe.py --discover               # figure out the request shape first
    python probe.py crate.txt                # then run the real coverage test

crate.txt format, one per line:
    Artist - Title
    # lines starting with # are ignored
"""

import argparse
import json
import os
import sqlite3
import sys
import time

import requests

HOST = "track-analysis.p.rapidapi.com"
BASE = f"https://{HOST}"
KEY = os.environ.get("RAPIDAPI_KEY", "")

# The exact query-param names aren't documented publicly; --discover finds the
# working combination empirically so we don't guess wrong for 200 requests.
PARAM_VARIANTS = [
    ("title", "artist"),
    ("track", "artist"),
    ("name", "artist"),
    ("q", None),
    ("query", None),
]
ENDPOINTS = ["/pktx/key-bpm", "/pktx/analysis"]


def headers():
    if not KEY:
        sys.exit("RAPIDAPI_KEY is not set. Get one from the RapidAPI dashboard.")
    return {"X-RapidAPI-Key": KEY, "X-RapidAPI-Host": HOST}


def call(endpoint, params):
    try:
        r = requests.get(BASE + endpoint, headers=headers(), params=params, timeout=15)
    except requests.RequestException as e:
        return None, f"network: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        return r.json(), None
    except ValueError:
        return None, f"non-JSON: {r.text[:200]}"


def discover():
    """Probe param shapes against a track that unambiguously exists."""
    artist, title = "Daft Punk", "Around the World"
    print(f"Probing with: {artist} - {title}\n")
    for ep in ENDPOINTS:
        for tkey, akey in PARAM_VARIANTS:
            params = {tkey: title}
            if akey:
                params[akey] = artist
            data, err = call(ep, params)
            shape = ",".join(params.keys())
            if err:
                print(f"  {ep:<20} [{shape:<14}] -> {err[:70]}")
            else:
                print(f"  {ep:<20} [{shape:<14}] -> 200 OK")
                print("    " + json.dumps(data, indent=2)[:900].replace("\n", "\n    "))
                print()
            time.sleep(0.4)
    print("\nPick the endpoint+params that returned real data, then set "
          "ENDPOINT/TKEY/AKEY below and run the coverage test.")


# --- set these from what --discover tells you -------------------------------
ENDPOINT = "/pktx/key-bpm"
TKEY, AKEY = "title", "artist"
# ----------------------------------------------------------------------------

# Fields we hope to find; we search case-insensitively so schema drift is survivable.
WANT = ("bpm", "tempo", "key", "camelot", "mode", "energy")


def extract(data):
    """Pull the fields we care about out of whatever shape came back."""
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return {}
    # unwrap one level of common envelopes
    for env in ("data", "result", "track", "analysis"):
        if env in data and isinstance(data[env], (dict, list)):
            inner = data[env]
            if isinstance(inner, list):
                inner = inner[0] if inner else {}
            if isinstance(inner, dict):
                data = {**data, **inner}
    flat = {k.lower(): v for k, v in data.items() if not isinstance(v, (dict, list))}
    return {k: flat[k] for k in WANT if k in flat and flat[k] not in (None, "")}


def db_open(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS cache (
        artist TEXT, title TEXT, ok INT, fields TEXT, raw TEXT,
        PRIMARY KEY (artist, title))""")
    return con


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("crate", nargs="?", help="file of 'Artist - Title' lines")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--db", default="cache.sqlite")
    ap.add_argument("--delay", type=float, default=0.3)
    args = ap.parse_args()

    if args.discover:
        return discover()
    if not args.crate:
        return ap.print_help()

    tracks = []
    with open(args.crate, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            artist, _, title = line.partition(" - ")
            if title:
                tracks.append((artist.strip(), title.strip()))
            else:
                print(f"  skipping unparseable line: {line}")

    con = db_open(args.db)
    hits = misses = cached = 0
    missing = []

    for artist, title in tracks:
        row = con.execute("SELECT ok, fields FROM cache WHERE artist=? AND title=?",
                          (artist, title)).fetchone()
        if row:
            cached += 1
            if row[0]:
                hits += 1
                print(f"  [cache] {artist} - {title}: {row[1]}")
            else:
                misses += 1
                missing.append(f"{artist} - {title}")
            continue

        data, err = call(ENDPOINT, {TKEY: title, AKEY: artist})
        fields = extract(data) if data else {}
        ok = bool(fields)
        con.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
                    (artist, title, int(ok), json.dumps(fields),
                     json.dumps(data) if data else (err or "")))
        con.commit()

        if ok:
            hits += 1
            print(f"  [ hit ] {artist} - {title}: {fields}")
        else:
            misses += 1
            missing.append(f"{artist} - {title}")
            print(f"  [ MISS] {artist} - {title}"
                  + (f"  ({err[:60]})" if err else ""))
        time.sleep(args.delay)

    total = hits + misses
    print(f"\n{'='*60}")
    print(f"  tracks tested : {total}   ({cached} served from cache)")
    if total:
        print(f"  coverage      : {hits}/{total} = {hits/total:.0%}")
    if missing:
        print(f"\n  no data for {len(missing)}:")
        for m in missing[:25]:
            print(f"    - {m}")
        if len(missing) > 25:
            print(f"    ... and {len(missing)-25} more")
    print(f"\n  cached to {args.db} — these are yours to keep even if the API isn't.")


if __name__ == "__main__":
    main()
