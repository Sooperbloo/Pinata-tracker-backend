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
  PINATA_BOT_TOKEN            your Discord bot's token. Used only to LOOK AT the channel on startup, so that after an
                              API restart the existing image message is found and edited instead of posting a second
                              one (it also deletes older duplicates). The bot must be able to read the channel and
                              manage messages in it. If not set, the API falls back to the saved message ID only.
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
BOT_TOKEN = os.environ.get("PINATA_BOT_TOKEN", "").strip()
DISCORD_API = os.environ.get("PINATA_DISCORD_API", "https://discord.com/api/v10").rstrip("/")
IMAGE_FILENAME = "pinata_tracker.png"
LOOKBACK_MESSAGES = 50  # how far back to search the channel for an existing image message

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
            files={"files[0]": (IMAGE_FILENAME, png, "image/png")},
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


def _find_existing(webhook_url):
    """
    Look in the webhook's channel for image messages this webhook already posted.
    Returns the newest one's ID (and deletes older duplicates), or None if there isn't one / can't look.
    """
    if not BOT_TOKEN:
        return None
    try:
        info = requests.get(webhook_url, timeout=15)          # webhook URL alone is enough to read its own info
        info.raise_for_status()
        hook = info.json()
        webhook_id, channel_id = hook["id"], hook["channel_id"]
        headers = {"Authorization": f"Bot {BOT_TOKEN}"}
        resp = requests.get(f"{DISCORD_API}/channels/{channel_id}/messages",
                            params={"limit": LOOKBACK_MESSAGES}, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"[PinataDiscord] Could not read channel to look for an existing image: "
                  f"HTTP {resp.status_code} {resp.text[:150]}")
            return None
        mine = [m for m in resp.json()
                if m.get("webhook_id") == webhook_id
                and any(a.get("filename") == IMAGE_FILENAME for a in m.get("attachments", []))]
        if not mine:
            return None
        mine.sort(key=lambda m: int(m["id"]), reverse=True)    # newest first
        for dupe in mine[1:]:
            requests.delete(f"{DISCORD_API}/channels/{channel_id}/messages/{dupe['id']}", headers=headers, timeout=15)
        if len(mine) > 1:
            print(f"[PinataDiscord] Removed {len(mine) - 1} duplicate image message(s)")
        return mine[0]["id"]
    except Exception as e:
        print(f"[PinataDiscord] Existing-message lookup failed: {e}")
        return None


def _push(webhook_url, png, ids):
    """Edit the existing message (remembered or found in the channel), or post a fresh one if there is none."""
    message_id = ids.get(webhook_url)
    if not message_id:
        message_id = _find_existing(webhook_url)
        if message_id:
            print("[PinataDiscord] Found the existing image message in the channel — editing it")
            ids[webhook_url] = message_id
            _save_ids(ids)
    if message_id:
        resp = _request("PATCH", f"{webhook_url}/messages/{message_id}", png)
        if resp.status_code == 200:
            return True
        if resp.status_code not in (404, 10008):
            print(f"[PinataDiscord] Edit failed: HTTP {resp.status_code} {resp.text[:200]}")
            return False
        print("[PinataDiscord] Old message is gone")
        ids.pop(webhook_url, None)
        found = _find_existing(webhook_url)
        if found and found != message_id:
            ids[webhook_url] = found
            _save_ids(ids)
            resp = _request("PATCH", f"{webhook_url}/messages/{found}", png)
            if resp.status_code == 200:
                return True
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
    if not BOT_TOKEN:
        print("[PinataDiscord] PINATA_BOT_TOKEN not set — after a restart a new image message may be posted "
              "if the saved message ID was lost")
    t = threading.Thread(target=_loop, args=(get_realms,), daemon=True, name="pinata-discord")
    t.start()
    print(f"[PinataDiscord] Started — updating {len(WEBHOOK_URLS)} webhook message(s)")
    return t
