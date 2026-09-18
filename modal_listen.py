"""
Runs listen.py's audio analysis on a container that actually has ffmpeg and
librosa - for the one deployment target (Vercel) that has neither and can't
easily get them without risking the whole app's build. app.py only calls
this when the in-process path in listen.py raises for a missing-dependency
reason; local dev (which does have both) never touches Modal at all, so
there's no added latency or cost for the common case.

    modal deploy modal_listen.py     <- one-time, or again after editing
                                         listen.py/analyze.py

Needs MODAL_TOKEN_ID / MODAL_TOKEN_SECRET set wherever app.py itself runs
(Vercel's project environment variables) - `modal token new` prints both.
"""

import modal

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install("librosa", "numpy", "soundfile")
    .add_local_python_source("analyze", "listen")
)

app = modal.App("slipmat-listen", image=image)


@app.function(timeout=60)
def analyze_recording(raw_bytes: bytes, suffix: str = ".webm"):
    """Same signature and return shape as listen.analyze_upload - this
    *is* that function, just running somewhere ffmpeg and librosa exist."""
    import listen
    return listen.analyze_upload(raw_bytes, suffix=suffix)
