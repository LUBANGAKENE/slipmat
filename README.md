# Slipmat

Photograph a vinyl record, get back the artist, album, and tracklist — with the
side positions (`A1`, `B3`) that tell you where to drop the needle.

Built for DJs coming to vinyl from digital. Rekordbox tells you the BPM and key
because it analysed the audio file. With vinyl there is no file, so Slipmat
starts from the object in your hands: it reads the text printed on the sleeve.

![status](https://img.shields.io/badge/status-work%20in%20progress-orange)

---

## What it does today

- Drag in a photo of the front sleeve, back cover, or centre label
- A vision model reads the printed text and returns structured data
- Falls back to MusicBrainz when the photo shows no tracklist
- Prefers vinyl pressings over CD releases, so you get `A1/B2` rather than `1,2,3`
- Runs on your phone over local wifi, shooting straight from the camera

## What it does not do yet

BPM and key lookup is built ([`analyze.py`](analyze.py) — Camelot conversion,
half/double-time candidates, pitch-fader math) but not yet wired into the
dashboard. The results table shows a placeholder column where it will land.

---

## Setup

Requires Python 3.10+.

```bash
git clone https://github.com/OWNER/slipmat.git
cd slipmat
pip install flask requests pillow opencv-python

cp .env.example .env      # then put your key in it
python app.py
```

Open <http://127.0.0.1:5000>. The server binds on all interfaces and prints a
LAN address, so you can open it on your phone on the same wifi and use the
camera directly — which is how you would actually use this next to the decks.

### API key

Vision runs through [OpenRouter](https://openrouter.ai). Put your key in `.env`:

```
OPENROUTER_API_KEY=sk-or-v1-...
VINYL_MODEL=google/gemini-2.5-flash
```

Roughly $0.001 per scan with Flash — about 1,000 records per dollar. Any
vision-capable OpenRouter model works; change `VINYL_MODEL` to swap.

---

## How it works

**1. Read, don't recall.** The prompt tells the model to transcribe what is
printed and return `null` for anything illegible. Without that instruction it
pattern-matches a familiar cover and recites a tracklist from training data —
which looks correct and is unverifiable.

**2. Sleeve first, database second.** The record in your hands is ground truth.
A database lookup can only guess *which pressing you own*, and the 1979 12"
has different timings from the 2016 reissue. MusicBrainz is consulted only when
the photos show no tracklist at all.

**3. The recogniser proposes, the database disposes.** When the fallback does
run, the model's output is a hypothesis, never an answer. Results are searched
strictly, then fuzzily (so a misread `Rumors` still finds *Rumours*), and each
candidate is tagged `exact` / `strong` / `weak`. That tag is the confidence
gate — it is why a cheap model is good enough.

```
 photos ──► vision model ──► artist / album / tracklist
                │
                └── no tracklist? ──► MusicBrainz ──► vinyl pressing preferred
```

---

## Files

| File | Role |
|---|---|
| `app.py` | Flask server, two routes |
| `vinyl.py` | The engine — vision call, prompt, fallback orchestration |
| `identify.py` | MusicBrainz search, fuzzy matching, vinyl-preference ranking |
| `analyze.py` | BPM/key → Camelot → pitch math (not yet wired in) |
| `templates/index.html` | The dashboard |
| `probe.py` | Standalone coverage tester for a BPM/key API |
| `scan.py` | Earlier Gemini-direct version, superseded by `vinyl.py` |

---

## Notes

- MusicBrainz throttles and returns 503 under load. This is normal; requests
  retry with backoff and degrade gracefully rather than losing a good scan.
  Set `MB_USER_AGENT` to something with your contact details.
- Photographing the **back cover** is both faster and more accurate than the
  database fallback — you get that exact pressing's tracklist.
- Barcodes only appear on post-1980s and reissue pressings, so they are an
  opportunistic shortcut rather than a primary path.

## License

MIT
