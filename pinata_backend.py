import os
import json
import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

REPORT_KEY = os.environ.get("PINATA_REPORT_KEY", "changeme")
REALMS = ["Elysium", "Arcane", "Cosmic"]

STATE_FILE_PATH = os.environ.get("STATE_FILE_PATH", "pinata_state.json")

DISABLED_MOD_VERSIONS = {
    v.strip() for v in os.environ.get("DISABLED_MOD_VERSIONS", "1.0.0").split(",") if v.strip()
}

SUSPICIOUS_JUMP_THRESHOLD = 15
CONSENSUS_WINDOW_SECONDS = 10
RATE_LIMIT_MAX_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60

_lock = threading.Lock()


def _default_state():
    return {realm: {"count": None, "updated_at": None, "reporter": None} for realm in REALMS}


def _load_state_from_disk():
    abs_path = os.path.abspath(STATE_FILE_PATH)
    print(f"[Pinata] STATE_FILE_PATH resolves to: {abs_path}")
    if os.path.exists(STATE_FILE_PATH):
        try:
            with open(STATE_FILE_PATH) as f:
                loaded = json.load(f)
            state = _default_state()
            for realm in REALMS:
                if realm in loaded:
                    state[realm] = loaded[realm]
            print(f"[Pinata] Restored state from {abs_path}: {state}")
            return state
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"[Pinata] Failed to load state file, starting fresh: {e}")
    else:
        print(f"[Pinata] No existing state file at {abs_path} — starting fresh")
    return _default_state()


def _save_state_to_disk():
    try:
        with open(STATE_FILE_PATH, "w") as f:
            json.dump(_state, f)
    except OSError as e:
        print(f"[Pinata] Failed to save state file: {e}")


_state = _load_state_from_disk()

_recent_reports = {realm: {} for realm in REALMS}
_rate_limit_log = {}

STALE_AFTER_SECONDS = 90


def _check_rate_limit(client_id):
    now = time.time()
    log = _rate_limit_log.setdefault(client_id, [])
    log[:] = [t for t in log if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(log) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    log.append(now)
    return True


def _prune_recent_reports(realm, now):
    stale_ids = [cid for cid, r in _recent_reports[realm].items() if now - r["time"] > CONSENSUS_WINDOW_SECONDS]
    for cid in stale_ids:
        del _recent_reports[realm][cid]


@app.route("/report", methods=["POST"])
def report():
    if request.headers.get("X-Api-Key") != REPORT_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    realm       = str(data.get("realm", ""))
    count       = data.get("count")
    player      = str(data.get("player", "unknown"))[:32]
    client_id   = data.get("client_id")
    mod_version = data.get("mod_version")

    if realm not in REALMS:
        return jsonify({"error": f"unknown realm '{realm}'"}), 400
    if not isinstance(count, int) or not (0 <= count <= 100):
        return jsonify({"error": "count must be an int 0-100"}), 400
    if not client_id or not isinstance(client_id, str):
        return jsonify({"error": "missing client_id — update your mod"}), 400
    if not mod_version or not isinstance(mod_version, str):
        return jsonify({"error": "missing mod_version — update your mod"}), 400
    if mod_version in DISABLED_MOD_VERSIONS:
        return jsonify({"error": f"mod version {mod_version} is disabled — please update"}), 403

    if not _check_rate_limit(client_id):
        return jsonify({"error": "rate limited"}), 429

    with _lock:
        now = time.time()
        previous = _state[realm]["count"]
        is_trusted_relay = client_id == "discord-bot-manual-relay"
        suspicious = (previous is not None
                      and abs(count - previous) >= SUSPICIOUS_JUMP_THRESHOLD
                      and not is_trusted_relay)

        if suspicious:
            _prune_recent_reports(realm, now)

            corroborated = any(
                cid != client_id and abs(r["count"] - count) <= 3
                for cid, r in _recent_reports[realm].items()
            )

            if not corroborated:
                _recent_reports[realm][client_id] = {"count": count, "time": now}
                print(f"[Pinata] Provisional (uncorroborated) report: realm={realm} count={count} "
                      f"player={player} client={client_id[:8]} (previous={previous})")
                return jsonify({"ok": True, "realm": realm, "count": count, "provisional": True})

            print(f"[Pinata] Corroborated jump: realm={realm} count={count} player={player} "
                  f"client={client_id[:8]} (previous={previous})")
            _recent_reports[realm].pop(client_id, None)

        _state[realm] = {"count": count, "updated_at": now, "reporter": player}
        _save_state_to_disk()

    print(f"[Pinata] Accepted report: realm={realm} count={count} player={player} client={client_id[:8]}")
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
