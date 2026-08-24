"""
Web dashboard for Slipmat.

    python app.py

Binds on all interfaces, so you can open it on your phone over the same wifi
and shoot the sleeve directly with the phone camera - which is how you'd
actually use this next to the decks.
"""

import socket
import traceback

from flask import Flask, jsonify, render_template, request

import vinyl

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024   # a few phone photos


@app.get("/")
def index():
    return render_template("index.html")


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
    print("    this machine : http://127.0.0.1:5000")
    print("    your phone   : http://%s:5000\n" % ip)
    app.run(host="0.0.0.0", port=5000, debug=False)
