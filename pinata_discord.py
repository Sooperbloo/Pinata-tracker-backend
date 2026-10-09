"""
pinata_discord.py — keeps one Discord message per webhook showing the current Pinata Tracker image.

How it works
  * A background thread wakes every few seconds and asks the API for the current counts.
  * It draws the tracker image (pinata_image.py). If the picture is identical to the last one sent, nothing happens.
  * If it changed, the existing webhook message is edited in place with the new image (no new messages).
    The message ID is remembered in a small JSON file so a restart edits the same message again.

Environment variables (set on Railway)
  PINATA_WEBHOOK_URLS         one or more Discord webhook URLs, comma-separated. Each gets the same image.
  PINATA_WEBHOOK_STATE_PATH   where message IDs are remembered (default pinata_webhook_messages.json).
                              Put this on a Railway volume if you want it to survive redeploys.
  PINATA_IMAGE_SCALE          image scale factor (default 4 -> 696x560).
"""
import hashlib
import json
import os
import threading
import time

import requests

from pinata_image import render_pinata_image

WEBHOOK_URLS = [u.strip() for u in os.environ.get("PINATA_WEBHOOK_URLS", "").split(",") if u.strip()]
STATE_PATH = os.environ.get("PINATA_WEBHOOK_STATE_PATH", "pinata_webhook_messages.json")
IMAGE_SCALE = int(os.environ.get("PINATA_IMAGE_SCALE", "4"))

CHECK_EVERY_SECONDS = 3      # how often to look for a change
MIN_SECONDS_BETWEEN_EDITS = 5  # never edit a message more often than this (Discord rate limits)


def _load_ids():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_ids(ids):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(ids, f)
    except OSError as e:
        print(f"[PinataDiscord] Could not save message ids: {e}")


def _request(method, url, png):
    """Send/edit a message carrying the image. Retries once if Discord says slow down."""
    for attempt in range(2):
        resp = requests.request(
            method, url,
            data={"payload_json": json.dumps({"attachments": []})} if method == "PATCH" else None,
            files={"files[0]": ("pinata_tracker.png", png, "image/png")},
            timeout=20,
        )
        if resp.status_code == 429 and attempt == 0:
            try:
                wait = float(resp.json().get("retry_after", 2))
            except ValueError:
                wait = 2
            time.sleep(min(wait, 10))
            continue
        return resp
    return resp


def _push(webhook_url, png, ids):
    """Edit the remembered message, or post a fresh one if there isn't one (or it was deleted)."""
    message_id = ids.get(webhook_url)
    if message_id:
        resp = _request("PATCH", f"{webhook_url}/messages/{message_id}", png)
        if resp.status_code == 200:
            return True
        if resp.status_code not in (404, 10008):
            print(f"[PinataDiscord] Edit failed: HTTP {resp.status_code} {resp.text[:200]}")
            return False
        print("[PinataDiscord] Old message is gone — posting a new one")
    resp = _request("POST", f"{webhook_url}?wait=true", png)
    if resp.status_code in (200, 204):
        try:
            ids[webhook_url] = resp.json()["id"]
            _save_ids(ids)
        except (ValueError, KeyError):
            pass
        return True
    print(f"[PinataDiscord] Post failed: HTTP {resp.status_code} {resp.text[:200]}")
    return False


def _loop(get_realms):
    ids = _load_ids()
    last_hash = None
    last_sent = 0.0
    while True:
        time.sleep(CHECK_EVERY_SECONDS)
        try:
            realms, maintenance = get_realms()
            png = render_pinata_image(realms, scale=IMAGE_SCALE, maintenance=maintenance)
            digest = hashlib.sha256(png).hexdigest()
            if digest == last_hash or time.time() - last_sent < MIN_SECONDS_BETWEEN_EDITS:
                continue
            ok = all([_push(url, png, ids) for url in WEBHOOK_URLS])  # list, not generator: try every webhook
            if ok:
                last_hash, last_sent = digest, time.time()
            else:
                last_sent = time.time()  # back off a little, retry next cycle
        except Exception as e:
            print(f"[PinataDiscord] Update loop error: {e}")


def start(get_realms):
    """
    get_realms() must return ([(realm_name, count_or_None, stale_bool), ...], maintenance_bool).
    Does nothing (and says so) if no webhook is configured.
    """
    if not WEBHOOK_URLS:
        print("[PinataDiscord] PINATA_WEBHOOK_URLS not set — Discord image updates are off")
        return None
    t = threading.Thread(target=_loop, args=(get_realms,), daemon=True, name="pinata-discord")
    t.start()
    print(f"[PinataDiscord] Started — updating {len(WEBHOOK_URLS)} webhook message(s)")
    return t
