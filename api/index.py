"""Vercel serverless entrypoint.

Vercel's Python runtime serves the module-level `app` WSGI callable. The real
application lives in app.py at the repo root; this file only puts that on the
import path and re-exports it. Everything else - routes, templates, the
/api/scan and /api/bpm handlers - is unchanged from running `python app.py`
locally.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401
