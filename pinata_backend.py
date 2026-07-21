import os
import json
import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

REPORT_KEY = os.environ.get("PINATA_REPORT_KEY", "changeme")
ADMIN_KEY = os.environ.get("PINATA_ADMIN_KEY", "changeme-admin")
KEYS_FILE_PATH = os.environ.get("KEYS_FILE_PATH", "pinata_keys.json")
ALLOWED_ADMIN_IPS = {
    ip.strip() for ip in os.environ.get("ALLOWED_ADMIN_IPS", "").split(",") if ip.strip()
}
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
            backup = loaded.get("pre_maintenance_backup")
            print(f"[Pinata] Restored state from {abs_path}: {state} maintenance={maintenance}")
            return state, maintenance, backup
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"[Pinata] Failed to load state file, starting fresh: {e}")
    else:
        print(f"[Pinata] No existing state file at {abs_path} — starting fresh")
    return _default_state(), False, None


def _save_state_to_disk():
    try:
        with open(STATE_FILE_PATH, "w") as f:
            json.dump({
                "realms": _state,
                "maintenance": _maintenance,
                "pre_maintenance_backup": _pre_maintenance_backup,
            }, f)
    except OSError as e:
        print(f"[Pinata] Failed to save state file: {e}")


_state, _maintenance, _pre_maintenance_backup = _load_state_from_disk()

import datetime
print(f"[Pinata] ===== BACKEND STARTED at {datetime.datetime.now(datetime.timezone.utc).isoformat()} UTC =====")

_rate_limit_log = {}


def _load_player_keys():
    if os.path.exists(KEYS_FILE_PATH):
        try:
            with open(KEYS_FILE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"[Pinata] Failed to load player keys, starting empty: {e}")
    return {}


def _save_player_keys():
    try:
        with open(KEYS_FILE_PATH, "w") as f:
            json.dump(_player_keys, f, indent=2)
    except OSError as e:
        print(f"[Pinata] Failed to save player keys: {e}")


_player_keys = _load_player_keys()  # {key: player_name}


def _identify_reporter(provided_key):
    """Returns a display name for who this key belongs to, or None if invalid."""
    if provided_key == REPORT_KEY:
        return "(shared key)"
    if provided_key in _player_keys:
        return _player_keys[provided_key]
    return None


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
    provided_key = request.headers.get("X-Api-Key")
    key_owner = _identify_reporter(provided_key)
    if key_owner is None:
        print(f"[Pinata] REJECTED (401 unauthorized): body={request.get_json(silent=True)}")
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    realm       = str(data.get("realm", ""))
    count       = data.get("count")
    player      = str(data.get("player", "unknown"))[:32]
    client_id   = data.get("client_id")
    mod_version = data.get("mod_version")

    print(f"[Pinata] Incoming report: key_owner={key_owner!r} realm={realm!r} count={count!r} player={player!r} "
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
        print(f"[Pinata] REJECTED (429): rate limited client_id={client_id!r} key_owner={key_owner!r}")
        return jsonify({"error": "rate limited"}), 429

    with _lock:
        now = time.time()
        _state[realm] = {"count": count, "updated_at": now, "reporter": player, "key_owner": key_owner}
        _save_state_to_disk()

    print(f"[Pinata] Accepted report: key_owner={key_owner!r} realm={realm} count={count} player={player} client={client_id[:8]}")
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


def _get_client_ip():
    # Railway sits behind a proxy, so the real client IP is in X-Forwarded-For,
    # not request.remote_addr (which would just show Railway's internal edge IP).
    # X-Forwarded-For can be a chain "client, proxy1, proxy2" — the first entry
    # is the original client.
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr


def _admin_authorized():
    if request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return False
    client_ip = _get_client_ip()
    if ALLOWED_ADMIN_IPS and client_ip not in ALLOWED_ADMIN_IPS:
        print(f"[Pinata] ADMIN REJECTED: correct key but IP {client_ip} not in allowlist")
        return False
    return True


@app.route("/admin/maintenance", methods=["POST"])
def admin_maintenance():
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401

    global _maintenance, _pre_maintenance_backup
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    reset_counts = bool(data.get("reset_counts", True))
    restore_counts = bool(data.get("restore_counts", True))

    with _lock:
        if enabled:
            if reset_counts:
                _pre_maintenance_backup = {realm: dict(_state[realm]) for realm in REALMS}
                for realm in REALMS:
                    _state[realm] = {"count": 0, "updated_at": time.time(), "reporter": "admin-maintenance"}
                print(f"[Pinata] ADMIN: backed up pre-maintenance state: {_pre_maintenance_backup}")
        else:
            if restore_counts and _pre_maintenance_backup is not None:
                for realm in REALMS:
                    if realm in _pre_maintenance_backup:
                        _state[realm] = _pre_maintenance_backup[realm]
                print(f"[Pinata] ADMIN: restored pre-maintenance state: {_pre_maintenance_backup}")
                _pre_maintenance_backup = None

        _maintenance = enabled
        _save_state_to_disk()

    print(f"[Pinata] ADMIN: maintenance set to {enabled} (reset_counts={reset_counts}, restore_counts={restore_counts})")
    return jsonify({"ok": True, "maintenance": _maintenance})


@app.route("/admin/keys", methods=["GET"])
def admin_list_keys():
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"keys": _player_keys})


@app.route("/admin/keys/add", methods=["POST"])
def admin_add_key():
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    player = str(data.get("player", "")).strip()
    if not player:
        return jsonify({"error": "missing player name"}), 400

    import secrets
    new_key = secrets.token_urlsafe(24)

    with _lock:
        _player_keys[new_key] = player
        _save_player_keys()

    print(f"[Pinata] ADMIN: issued new key for player={player!r}")
    return jsonify({"ok": True, "player": player, "key": new_key})


@app.route("/admin/keys/revoke", methods=["POST"])
def admin_revoke_key():
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    key = data.get("key")
    player = data.get("player")

    with _lock:
        removed = []
        if key and key in _player_keys:
            removed.append((key, _player_keys.pop(key)))
        elif player:
            matching = [k for k, p in _player_keys.items() if p == player]
            for k in matching:
                removed.append((k, _player_keys.pop(k)))
        _save_player_keys()

    print(f"[Pinata] ADMIN: revoked keys: {removed}")
    return jsonify({"ok": True, "revoked": [p for _, p in removed]})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
