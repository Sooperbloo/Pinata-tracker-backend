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

# Party (pinata event) status — deliberately in-memory only, not persisted.
# This is transient event data (a party lasts minutes at most), so losing it
# on a restart is a non-issue, unlike vote counts which need to survive.
_party_state = {realm: {"active": False, "llama_hits": [], "countdown": None, "updated_at": 0} for realm in REALMS}
PARTY_STALE_AFTER_SECONDS = 60

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

REPORT_COUNTS_PATH = os.environ.get("REPORT_COUNTS_PATH", "pinata_report_counts.json")


def _load_report_counts():
    if os.path.exists(REPORT_COUNTS_PATH):
        try:
            with open(REPORT_COUNTS_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"[Pinata] Failed to load report counts, starting empty: {e}")
    return {}


def _save_report_counts():
    try:
        with open(REPORT_COUNTS_PATH, "w") as f:
            json.dump(_report_counts, f, indent=2)
    except OSError as e:
        print(f"[Pinata] Failed to save report counts: {e}")


_report_counts = _load_report_counts()  # {key_owner: accepted_report_count}

# Key-management unlock state — deliberately in-memory only (resets on restart,
# safe-by-default) rather than persisted. {key: expiry_timestamp}, plus one
# global expiry for "unlock everyone".
_unlocked_keys = {}
_global_unlock_until = 0


def _resolve_key(player=None, key=None):
    """Turns a player name or literal key into the actual key string, or None."""
    if key:
        return key
    if player:
        for k, p in _player_keys.items():
            if p.lower() == player.lower():
                return k
    return None


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

    data = request.get_json(silent=True) or {}
    realm       = str(data.get("realm", ""))
    count       = data.get("count")
    player      = str(data.get("player", "unknown"))[:32]
    client_id   = data.get("client_id")
    mod_version = data.get("mod_version")

    key_owner = _identify_reporter(provided_key)
    if key_owner is None:
        # Not the shared key and not a known per-player key yet. Rather than
        # reject outright, self-register it — mirrors how client_id already
        # works: each install generates its own random value on first launch,
        # and its first contact with the backend is what "claims" it. No
        # manual key distribution needed. Still requires a plausible,
        # well-formed key (not blank) and a valid-looking request shape.
        if provided_key and isinstance(provided_key, str) and len(provided_key) >= 16 \
                and client_id and isinstance(client_id, str) and mod_version not in (None, ""):
            with _lock:
                _player_keys[provided_key] = player
                _save_player_keys()
            key_owner = player
            print(f"[Pinata] Self-registered new key for player={player!r} client={str(client_id)[:8]}")
        else:
            print(f"[Pinata] REJECTED (401 unauthorized): body={data}")
            return jsonify({"error": "unauthorized"}), 401

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
        _report_counts[key_owner] = _report_counts.get(key_owner, 0) + 1
        _save_state_to_disk()
        _save_report_counts()

    print(f"[Pinata] Accepted report: key_owner={key_owner!r} realm={realm} count={count} player={player} client={client_id[:8]}")
    return jsonify({"ok": True, "realm": realm, "count": count})


@app.route("/party_report", methods=["POST"])
def party_report():
    provided_key = request.headers.get("X-Api-Key")

    data = request.get_json(silent=True) or {}
    realm       = str(data.get("realm", ""))
    active      = bool(data.get("active", False))
    llama_hits  = data.get("llama_hits", [])
    countdown   = data.get("countdown")
    client_id   = data.get("client_id")
    mod_version = data.get("mod_version")

    if realm not in REALMS:
        return jsonify({"error": f"unknown realm '{realm}'"}), 400
    if not client_id or not isinstance(client_id, str):
        return jsonify({"error": "missing client_id"}), 400
    if not mod_version or not isinstance(mod_version, str):
        return jsonify({"error": "missing mod_version"}), 400
    if mod_version in DISABLED_MOD_VERSIONS:
        return jsonify({"error": f"mod version {mod_version} is disabled — please update"}), 403
    if not isinstance(llama_hits, list) or not all(isinstance(h, int) for h in llama_hits):
        return jsonify({"error": "llama_hits must be a list of ints"}), 400
    if countdown is not None and not isinstance(countdown, (int, float)):
        return jsonify({"error": "countdown must be a number or null"}), 400

    key_owner = _identify_reporter(provided_key)
    if key_owner is None:
        if provided_key and isinstance(provided_key, str) and len(provided_key) >= 16:
            with _lock:
                _player_keys[provided_key] = data.get("player", "unknown")
                _save_player_keys()
            key_owner = data.get("player", "unknown")
        else:
            return jsonify({"error": "unauthorized"}), 401

    if not _check_rate_limit(client_id):
        return jsonify({"error": "rate limited"}), 429

    with _lock:
        _party_state[realm] = {
            "active": active,
            "llama_hits": llama_hits,
            "countdown": countdown,
            "updated_at": time.time(),
        }

    print(f"[Pinata] Party report: realm={realm} active={active} llama_hits={llama_hits} "
          f"countdown={countdown} key_owner={key_owner!r}")
    return jsonify({"ok": True})


@app.route("/party_status", methods=["GET"])
def party_status():
    now = time.time()
    out = {}
    for realm, entry in _party_state.items():
        stale = (now - entry["updated_at"]) > PARTY_STALE_AFTER_SECONDS
        out[realm] = {
            "active": entry["active"] and not stale,
            "llama_hits": entry["llama_hits"] if not stale else [],
            "countdown": entry["countdown"] if not stale else None,
        }
    return jsonify(out)


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


@app.route("/admin/keys/lookup", methods=["GET"])
def admin_lookup_key():
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401

    key = request.args.get("key")
    player = request.args.get("player")

    if key:
        owner = _player_keys.get(key)
        if owner is None and key == REPORT_KEY:
            owner = "(shared key)"
        return jsonify({"key": key, "player": owner, "found": owner is not None})

    if player:
        matches = [{"key": k, "player": p} for k, p in _player_keys.items() if p.lower() == player.lower()]
        return jsonify({"player": player, "keys": matches, "found": len(matches) > 0})

    return jsonify({"error": "provide a 'key' or 'player' query parameter"}), 400


@app.route("/admin/leaderboard", methods=["GET"])
def admin_leaderboard():
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401

    ranked = sorted(_report_counts.items(), key=lambda item: item[1], reverse=True)
    return jsonify({"leaderboard": [{"player": p, "reports": c} for p, c in ranked]})


@app.route("/admin/unlock", methods=["POST"])
def admin_unlock():
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401

    global _global_unlock_until
    data = request.get_json(silent=True) or {}
    scope = data.get("scope")
    player = data.get("player")
    key = data.get("key")
    duration_minutes = data.get("duration_minutes", 15)

    try:
        duration_minutes = float(duration_minutes)
    except (TypeError, ValueError):
        duration_minutes = 15

    expiry = time.time() + duration_minutes * 60

    if scope == "all" or (not player and not key and not scope):
        _global_unlock_until = expiry
        print(f"[Pinata] ADMIN: unlocked key management for EVERYONE for {duration_minutes} min")
        return jsonify({"ok": True, "scope": "all", "expires_in_minutes": duration_minutes})

    resolved_key = _resolve_key(player=player, key=key)
    if not resolved_key:
        return jsonify({"error": "no matching player/key found"}), 404

    _unlocked_keys[resolved_key] = expiry
    owner = _player_keys.get(resolved_key, "(shared key)")
    print(f"[Pinata] ADMIN: unlocked key management for {owner!r} for {duration_minutes} min")
    return jsonify({"ok": True, "player": owner, "expires_in_minutes": duration_minutes})


@app.route("/admin/lock", methods=["POST"])
def admin_lock():
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401

    global _global_unlock_until
    data = request.get_json(silent=True) or {}
    scope = data.get("scope")
    player = data.get("player")
    key = data.get("key")

    if scope == "all" or (not player and not key and not scope):
        _global_unlock_until = 0
        _unlocked_keys.clear()
        print("[Pinata] ADMIN: locked key management for EVERYONE")
        return jsonify({"ok": True, "scope": "all"})

    resolved_key = _resolve_key(player=player, key=key)
    if not resolved_key:
        return jsonify({"error": "no matching player/key found"}), 404

    _unlocked_keys.pop(resolved_key, None)
    owner = _player_keys.get(resolved_key, "(shared key)")
    print(f"[Pinata] ADMIN: locked key management for {owner!r}")
    return jsonify({"ok": True, "player": owner})


@app.route("/keys/unlock_status", methods=["GET"])
def keys_unlock_status():
    # Public — the mod checks its OWN key's status using its own X-Api-Key,
    # no admin auth needed since it can only ever check itself.
    provided_key = request.headers.get("X-Api-Key")
    now = time.time()

    if now < _global_unlock_until:
        return jsonify({"unlocked": True, "expires_in_seconds": int(_global_unlock_until - now)})

    expiry = _unlocked_keys.get(provided_key)
    if expiry and now < expiry:
        return jsonify({"unlocked": True, "expires_in_seconds": int(expiry - now)})

    return jsonify({"unlocked": False})


@app.route("/version", methods=["GET"])
def latest_version():
    # Public, no auth — the mod checks this on startup to show an "update
    # available" note. Update LATEST_MOD_VERSION on Railway whenever you
    # ship a new release, no code change/redeploy needed for this alone.
    return jsonify({
        "latest": os.environ.get("LATEST_MOD_VERSION", "1.0.3"),
        "message": os.environ.get("LATEST_MOD_VERSION_MESSAGE", ""),
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
