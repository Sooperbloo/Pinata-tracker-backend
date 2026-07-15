"""
pinata_backend.py
Tiny backend for the Minecraft "Pinata Tracker" Fabric mod.

Flow:
  - Every client running the mod POSTs its current realm + vote count
    whenever it reads a change off the scoreboard.
  - All clients GET /counts every few seconds to render the shared
    HUD (so a player on Elysium can also see Arcane/Cosmic progress).
  - The Discord bot polls GET /counts on its own timer and feeds the
    numbers into the existing send_vote_update() pipeline — this
    service does NOT talk to Discord directly.

Deploy: Railway (or fps.ms Pterodactyl). Needs one env var:
  PINATA_REPORT_KEY - shared secret baked into the mod config,
                       required on every /report call.

PERSISTENCE: state is written to STATE_FILE_PATH on every accepted report
and reloaded on startup. Point this at a Railway Volume mount (e.g. /data)
so it survives redeploys/restarts — without a volume, this path lives on
the container's normal ephemeral disk and gets wiped on every redeploy
just like the old in-memory-only version did.

  1. Railway dashboard -> your service -> Settings -> Volumes -> add one,
     mount path e.g. /data
  2. Set env var STATE_FILE_PATH=/data/pinata_state.json
  3. Redeploy once to create the volume, then it persists from then on

Run locally:  python pinata_backend.py
Run in prod:  gunicorn pinata_backend:app --bind 0.0.0.0:$PORT
"""

import os
import json
import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

REPORT_KEY = os.environ.get("PINATA_REPORT_KEY", "changeme")
REALMS = ["Elysium", "Arcane", "Cosmic"]

# Falls back to a local path if no volume is mounted yet — that path just
# won't survive a redeploy, same as before, until STATE_FILE_PATH is set to
# somewhere on an actual Railway Volume.
STATE_FILE_PATH = os.environ.get("STATE_FILE_PATH", "pinata_state.json")

# ── State ──
# realm -> {"count": int|None, "updated_at": float|None, "reporter": str|None}
_lock = threading.Lock()


def _default_state():
    return {realm: {"count": None, "updated_at": None, "reporter": None} for realm in REALMS}


def _load_state_from_disk():
    if os.path.exists(STATE_FILE_PATH):
        try:
            with open(STATE_FILE_PATH) as f:
                loaded = json.load(f)
            state = _default_state()
            for realm in REALMS:
                if realm in loaded:
                    state[realm] = loaded[realm]
            print(f"[Pinata] Restored state from {STATE_FILE_PATH}: {state}")
            return state
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"[Pinata] Failed to load state file, starting fresh: {e}")
    return _default_state()


def _save_state_to_disk():
    try:
        with open(STATE_FILE_PATH, "w") as f:
            json.dump(_state, f)
    except OSError as e:
        print(f"[Pinata] Failed to save state file: {e}")


_state = _load_state_from_disk()

STALE_AFTER_SECONDS = 90     # HUD should show a realm as "stale" past this


@app.route("/report", methods=["POST"])
def report():
    if request.headers.get("X-Api-Key") != REPORT_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    realm  = str(data.get("realm", ""))
    count  = data.get("count")
    player = str(data.get("player", "unknown"))[:32]

    if realm not in REALMS:
        return jsonify({"error": f"unknown realm '{realm}'"}), 400
    if not isinstance(count, int) or not (0 <= count <= 100):
        return jsonify({"error": "count must be an int 0-100"}), 400

    with _lock:
        _state[realm] = {"count": count, "updated_at": time.time(), "reporter": player}
        _save_state_to_disk()

    print(f"[Pinata] Accepted report: realm={realm} count={count} player={player}")
    return jsonify({"ok": True, "realm": realm, "count": count})


@app.route("/counts", methods=["GET"])
def counts():
    now = time.time()
    with _lock:
        out = {}
        for realm, entry in _state.items():
            updated_at = entry["updated_at"]
            out[realm] = {
                "count": entry["count"],
                "updated_at": updated_at,
                "stale": (updated_at is None) or (now - updated_at > STALE_AFTER_SECONDS),
                "reporter": entry["reporter"],
            }
    return jsonify(out)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
