import os
import json
import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

REPORT_KEY = os.environ.get("PINATA_REPORT_KEY", "changeme")
ADMIN_KEY = os.environ.get("PINATA_ADMIN_KEY", "changeme-admin")
REALMS = ["Elysium", "Arcane", "Cosmic"]

STATE_FILE_PATH = os.environ.get("STATE_FILE_PATH", "pinata_state.json")

DISABLED_MOD_VERSIONS = {
    v.strip() for v in os.environ.get("DISABLED_MOD_VERSIONS", "1.0.0").split(",") if v.strip()
}

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
                if realm in loaded.get("realms", loaded):
                    state[realm] = loaded.get("realms", loaded)[realm]
            maintenance = loaded.get("maintenance", False)
            print(f"[Pinata] Restored state from {abs_path}: {state} maintenance={maintenance}")
            return state, maintenance
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"[Pinata] Failed to load state file, starting fresh: {e}")
    else:
        print(f"[Pinata] No existing state file at {abs_path} — starting fresh")
    return _default_state(), False


def _save_state_to_disk():
    try:
        with open(STATE_FILE_PATH, "w") as f:
            json.dump({"realms": _state, "maintenance": _maintenance}, f)
    except OSError as e:
        print(f"[Pinata] Failed to save state file: {e}")


_state, _maintenance = _load_state_from_disk()

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


@app.route("/report", methods=["POST"])
def report():
    if request.headers.get("X-Api-Key") != REPORT_KEY:
        print(f"[Pinata] REJECTED (401 unauthorized): body={request.get_json(silent=True)}")
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    realm       = str(data.get("realm", ""))
    count       = data.get("count")
    player      = str(data.get("player", "unknown"))[:32]
    client_id   = data.get("client_id")
    mod_version = data.get("mod_version")

    print(f"[Pinata] Incoming report: realm={realm!r} count={count!r} player={player!r} "
          f"client_id={client_id!r} mod_version={mod_version!r} raw={data}")

    if realm not in REALMS:
        print(f"[Pinata] REJECTED (400): unknown realm {realm!r}")
        return jsonify({"error": f"unknown realm '{realm}'"}), 400
    if not isinstance(count, int) or not (0 <= count <= 100):
        print(f"[Pinata] REJECTED (400): invalid count {count!r}")
        return jsonify({"error": "count must be an int 0-100"}), 400
    if not client_id or not isinstance(client_id, str):
        print(f"[Pinata] REJECTED (400): missing/invalid client_id {client_id!r}")
        return jsonify({"error": "missing client_id — update your mod"}), 400
    if not mod_version or not isinstance(mod_version, str):
        print(f"[Pinata] REJECTED (400): missing/invalid mod_version {mod_version!r}")
        return jsonify({"error": "missing mod_version — update your mod"}), 400
    if mod_version in DISABLED_MOD_VERSIONS:
        print(f"[Pinata] REJECTED (403): mod_version {mod_version!r} is disabled")
        return jsonify({"error": f"mod version {mod_version} is disabled — please update"}), 403

    if _maintenance:
        print(f"[Pinata] REJECTED (503): under maintenance, ignoring report from {player!r}")
        return jsonify({"error": "under maintenance"}), 503

    if not _check_rate_limit(client_id):
        print(f"[Pinata] REJECTED (429): rate limited client_id={client_id!r}")
        return jsonify({"error": "rate limited"}), 429

    with _lock:
        now = time.time()
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


@app.route("/maintenance", methods=["GET"])
def maintenance_status():
    return jsonify({"enabled": _maintenance})


@app.route("/admin/maintenance", methods=["POST"])
def admin_maintenance():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "unauthorized"}), 401

    global _maintenance
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    reset_counts = bool(data.get("reset_counts", True))

    with _lock:
        _maintenance = enabled
        if enabled and reset_counts:
            for realm in REALMS:
                _state[realm] = {"count": 0, "updated_at": time.time(), "reporter": "admin-maintenance"}
        _save_state_to_disk()

    print(f"[Pinata] ADMIN: maintenance set to {enabled} (reset_counts={reset_counts})")
    return jsonify({"ok": True, "maintenance": _maintenance})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
