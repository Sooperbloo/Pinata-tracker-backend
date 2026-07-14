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

Run locally:  python pinata_backend.py
Run in prod:  gunicorn pinata_backend:app --bind 0.0.0.0:$PORT
"""

import os
import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

REPORT_KEY = os.environ.get("PINATA_REPORT_KEY", "changeme")
REALMS = ["Elysium", "Arcane", "Cosmic"]

# ── State ──
# realm -> {"count": int|None, "updated_at": float|None, "reporter": str|None}
_lock = threading.Lock()
_state = {realm: {"count": None, "updated_at": None, "reporter": None} for realm in REALMS}

STALE_AFTER_SECONDS = 90     # HUD should show a realm as "stale" past this
MAX_PLAUSIBLE_DROP   = 5     # a report can't drop the count by more than this...
RESET_FLOOR          = 95    # ...unless the previous count was this high (pinata just fired)


def _sanity_check(realm: str, new_count: int) -> bool:
    """Reject reports that look spoofed or glitched, allow the 100->0 reset."""
    prev = _state[realm]["count"]
    if prev is None:
        return True
    if new_count >= prev:
        return True
    dropped = prev - new_count
    if dropped <= MAX_PLAUSIBLE_DROP:
        return True
    if prev >= RESET_FLOOR and new_count <= 5:
        # Vote party fired and reset back down near 0 — legit.
        return True
    return False


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
        if not _sanity_check(realm, count):
            print(f"[Pinata] REJECTED report: realm={realm} count={count} player={player} "
                  f"(kept existing {_state[realm]['count']})")
            return jsonify({"error": "rejected (implausible drop)", "kept": _state[realm]["count"]}), 200
        _state[realm] = {"count": count, "updated_at": time.time(), "reporter": player}

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
