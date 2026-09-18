"""
BPM and key measured off actual audio, not looked up from a catalogue.

    python listen.py clip.webm

A Shazam-style recording - the phone's mic held up to the turntable - answers
a question the lookup chain in bpm.py cannot: not what tempo *a* digital
master runs at, but what this exact pressing, at this exact platter and pitch
setting, is actually playing right now. No network, no matching, no misses -
just whatever librosa can hear in the clip.

Tempo comes from librosa's own beat tracker. Key does not - librosa has no
key estimator built in - so it's a textbook Krumhansl-Schmuckler match: average
the clip's chroma into one 12-bin pitch-class profile, correlate it against
the 24 rotated major/minor key profiles, and take the best fit. Good enough to
land on the right key or its relative most of the time; nowhere near what a
model trained for this would do, which is the tradeoff for needing nothing
heavier than librosa already sitting in the venv.
"""

import os
import subprocess
import sys
import tempfile

import analyze

# Krumhansl-Kessler key profiles: the classic empirical weighting of how much
# each pitch class "belongs" to a major/minor key built on the tonic at
# index 0. Correlating a clip's own chroma against every rotation of these
# is the standard cheap key estimator when nothing fancier is available.
# Plain lists, not arrays: numpy is imported lazily below (see _import_libs),
# so this module itself stays importable - and app.py working at all - on a
# deployment that never installed the audio stack.
_MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
_NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

MIN_SECONDS = 6.0   # shorter than this and the beat tracker has too little
                    # to lock onto - better to say so than guess.


def _import_libs():
    """librosa (and the numpy it needs) only when a clip actually has to be
    analysed. Vercel's function bundle doesn't carry them - the whole app
    would fail to even start if this were a module-level import instead -
    so a request here fails on its own with a clear reason instead of
    taking every other route down with it.
    """
    try:
        import numpy as np
        import librosa
        return np, librosa
    except ImportError as exc:
        raise RuntimeError(
            "audio analysis isn't available on this server (%s) - "
            "this feature needs librosa/numpy installed alongside it" % exc)


def _detect_tempo(np, librosa, y, sr):
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    return round(bpm, 1) if bpm > 0 else None


def _detect_key(np, librosa, y, sr):
    """Pitch-class profile of the clip, matched against all 24 rotated
    major/minor templates. Returns (note_name, mode_float) - mode_float
    follows analyze.to_camelot's convention (1.0 major, 0.0 minor).
    """
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    profile = chroma.mean(axis=1)
    if not np.any(profile):
        return None, None

    best_score, best_note, best_mode = -2.0, None, None
    for i in range(12):
        for template, mode in ((_MAJOR_PROFILE, 1.0), (_MINOR_PROFILE, 0.0)):
            score = np.corrcoef(profile, np.roll(template, i))[0, 1]
            if score > best_score:
                best_score, best_note, best_mode = score, _NOTES[i], mode
    return best_note, best_mode


def analyze_clip(path, duration=None):
    """A recorded clip on disk -> the same shape bpm.py's lookup chain
    produces, so it can be saved and rendered through the identical path.
    Returns None if the clip is unusably short or silent.
    """
    np, librosa = _import_libs()
    y, sr = librosa.load(path, sr=22050, mono=True, duration=duration)
    if librosa.get_duration(y=y, sr=sr) < MIN_SECONDS:
        return None

    bpm_val = _detect_tempo(np, librosa, y, sr)
    if not bpm_val:
        return None
    note, mode = _detect_key(np, librosa, y, sr)

    return {
        "bpm": bpm_val,
        "bpm_alternatives": analyze.tempo_candidates(bpm_val),
        "key": note,
        "mode": "major" if mode is not None and mode >= 0.5 else
                "minor" if mode is not None else None,
        "camelot": analyze.to_camelot(note, mode) if note and mode is not None else None,
        "source": "recorded",
    }


def analyze_upload(raw_bytes, suffix=".webm"):
    """A browser recording's raw bytes -> the same shape as analyze_clip().

    A phone's MediaRecorder hands back whatever container its browser
    prefers - webm/opus on Chrome and Firefox, mp4/aac on Safari - and
    librosa's own format sniffing is not reliable enough to bet a feature on.
    Going through ffmpeg explicitly makes the format a non-issue: whatever
    comes in, a plain mono WAV comes out, and that's all librosa ever sees.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in" + suffix)
        wav = os.path.join(tmp, "out.wav")
        with open(src, "wb") as f:
            f.write(raw_bytes)

        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "22050", wav],
                check=True, capture_output=True, timeout=30)
        except FileNotFoundError:
            raise RuntimeError("ffmpeg is not installed - required to decode a recording")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("could not decode that recording: %s"
                                % exc.stderr.decode(errors="replace")[-300:])

        return analyze_clip(wav)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python listen.py clip.webm")
    feat = analyze_clip(sys.argv[1])
    if not feat:
        sys.exit("could not measure a tempo from that clip - too short, or too quiet")
    print("\n  %s BPM   %s   (%s %s)   via %s\n"
          % (feat["bpm"], feat.get("camelot") or "?",
             feat.get("key") or "?", feat.get("mode") or "", feat["source"]))
