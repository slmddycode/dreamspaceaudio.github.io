#!/usr/bin/env python3
"""
yt_rotate.py - keeps a 24/7 YouTube stream in chunks shorter than 12 hours.

YouTube only saves (archives) a live broadcast as a video if it is under
12 hours long, and only saved broadcasts count toward the Partner Program's
watch hours. This script ends the running broadcast and starts a fresh public
one on the same stream key, so every chunk is saved and counted. Your server's
stream (the yt-stream service) keeps running the whole time.

Commands (run them from the folder this file is in):
  python3 yt_rotate.py setup          ask for your settings and save them
  python3 yt_rotate.py login          connect to your YouTube channel (once)
  python3 yt_rotate.py status         show your stream and live broadcasts
  python3 yt_rotate.py rotate         start a fresh broadcast right now
  python3 yt_rotate.py auto           what the timer runs: starts a fresh broadcast
                                      only when the live one is 11+ hours old,
                                      or when nothing is live
  python3 yt_rotate.py install-timer  run 'auto' every 30 minutes from now on
  python3 yt_rotate.py remove-timer   stop the timer

Add --dry-run to 'rotate' or 'auto' to see what would happen without changing anything.
Needs only Python 3, which your server already has. No extra installs.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
TOKEN_PATH = os.path.join(HERE, "token.json")
STATE_PATH = os.path.join(HERE, "state.json")

API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube"

DEFAULT_TITLE = "Heavy Rain for Deep Sleep | Black Screen | 24/7"
DEFAULT_MAX_HOURS = 11

SERVICE_FILE = "/etc/systemd/system/yt-rotate.service"
TIMER_FILE = "/etc/systemd/system/yt-rotate.timer"


# ---------------------------------------------------------------- helpers

def log(msg):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def die(msg):
    print(f"\nSTOPPED: {msg}\n", file=sys.stderr, flush=True)
    sys.exit(1)


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        die(f"{os.path.basename(path)} is not valid JSON. Fix it with nano, or delete it and run setup again.")


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)          # only you can read it: it holds secrets
    os.replace(tmp, path)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    s = re.sub(r"\.\d+", "", s).replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(s)


def hours_text(h):
    whole = int(h)
    mins = int(round((h - whole) * 60))
    if mins == 60:
        whole, mins = whole + 1, 0
    return f"{whole}h {mins:02d}m"


def mask(key):
    return ("..." + key[-4:]) if key else "(none)"


class ApiError(Exception):
    def __init__(self, status, payload, raw):
        self.status = status
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):                      # YouTube API style error
            errors = err.get("errors") or [{}]
            self.reason = errors[0].get("reason", "")
            self.message = err.get("message", raw[:300])
        elif isinstance(err, str):                     # Google login style error
            self.reason = err
            self.message = payload.get("error_description", err)
        else:
            self.reason = ""
            self.message = raw[:300]
        super().__init__(f"HTTP {status} {self.reason}: {self.message}".strip())


def http(method, url, params=None, body=None, data=None, headers=None, retries=3):
    headers = dict(headers or {})
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
            if e.code >= 500 and attempt < retries:     # YouTube hiccup: wait and retry
                time.sleep(5 * (attempt + 1))
                continue
            raise ApiError(e.code, payload, raw)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:                       # network hiccup: wait and retry
                time.sleep(5 * (attempt + 1))
                continue
            raise ApiError(0, {}, f"network problem: {e}")


def post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    return http("POST", url, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}, retries=1)


# ---------------------------------------------------------------- config and login

def load_config():
    cfg = load_json(CONFIG_PATH)
    if not cfg:
        die("No settings yet. Run: python3 yt_rotate.py setup")
    for key in ("client_id", "client_secret", "title"):
        if not cfg.get(key):
            die(f"'{key}' is missing from config.json. Run: python3 yt_rotate.py setup")
    return cfg


def access_token(cfg):
    tok = load_json(TOKEN_PATH)
    if not tok or not tok.get("refresh_token"):
        die("Not connected to YouTube yet. Run: python3 yt_rotate.py login")
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 120:
        return tok["access_token"]
    try:
        r = post_form(TOKEN_URL, {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        })
    except ApiError as e:
        if e.reason == "invalid_grant":
            die("Your YouTube connection has expired or was removed. Run: python3 yt_rotate.py login\n"
                "(If this keeps happening every 7 days, your Google app is still in 'Testing'. "
                "Publish it - see step 1 of the guide.)")
        raise
    tok["access_token"] = r["access_token"]
    tok["expires_at"] = time.time() + int(r.get("expires_in", 3600))
    save_json(TOKEN_PATH, tok)
    return tok["access_token"]


class YouTube:
    def __init__(self, cfg):
        self.cfg = cfg

    def call(self, method, path, params=None, body=None):
        headers = {"Authorization": "Bearer " + access_token(self.cfg)}
        return http(method, API + path, params=params, body=body, headers=headers)

    def upload(self, path, params, data, content_type):
        headers = {"Authorization": "Bearer " + access_token(self.cfg), "Content-Type": content_type}
        return http("POST", UPLOAD_API + path, params=params, data=data, headers=headers)


def ask(prompt, current=None, secret=False):
    shown = ""
    if current:
        shown = f" [{mask(current) if secret else current}]"
    answer = input(f"{prompt}{shown}: ").strip()
    return answer or (current or "")


def cmd_setup(_args):
    cfg = load_json(CONFIG_PATH, {}) or {}
    print("\nPress Enter to keep the value shown in [brackets].\n")
    cfg["client_id"] = ask("Client ID (from Google Cloud)", cfg.get("client_id"))
    cfg["client_secret"] = ask("Client secret (from Google Cloud)", cfg.get("client_secret"), secret=True)
    cfg["stream_key"] = ask("Stream key (YouTube Studio > Go live > Stream)", cfg.get("stream_key"), secret=True)
    while True:
        title = ask("Title for each broadcast", cfg.get("title") or DEFAULT_TITLE)
        if len(title) > 100:
            print(f"  That title is {len(title)} characters. YouTube allows 100 at most - try again.")
        elif "<" in title or ">" in title:
            print("  YouTube doesn't allow < or > in titles - try again.")
        else:
            cfg["title"] = title
            break
    cfg["description"] = ask("Description (one line is fine; edit config.json later for more)",
                             cfg.get("description", ""))
    cfg["thumbnail"] = ask("Thumbnail file (Enter for the default)",
                           cfg.get("thumbnail") or os.path.join(HERE, "thumbnail.png"))
    cfg.setdefault("privacy", "public")
    cfg.setdefault("max_hours", DEFAULT_MAX_HOURS)
    if not cfg["client_id"] or not cfg["client_secret"]:
        die("Client ID and client secret are both needed. Run setup again.")
    save_json(CONFIG_PATH, cfg)
    if not os.path.exists(cfg["thumbnail"]):
        print(f"\nNote: no thumbnail file at {cfg['thumbnail']} yet. Broadcasts will still work, "
              "but without your thumbnail until you put it there.")
    print(f"\nSaved to {CONFIG_PATH}. Next: python3 yt_rotate.py login\n")


def cmd_login(_args):
    cfg = load_config()
    try:
        r = post_form(DEVICE_URL, {"client_id": cfg["client_id"], "scope": SCOPE})
    except ApiError as e:
        if e.reason in ("invalid_client", "unauthorized_client"):
            die("Google didn't accept the client ID. Check it in config.json, and check the OAuth client's "
                "type is 'TVs and Limited Input devices'.")
        raise
    url = r.get("verification_url") or r.get("verification_uri") or "https://www.google.com/device"
    print("\n" + "=" * 60)
    print(f"  1. On your Mac or phone, open:  {url}")
    print(f"  2. Type this code:              {r['user_code']}")
    print("  3. Sign in with the Google account that owns your channel,")
    print("     pick the channel, and allow access.")
    print("  If Google says the app isn't verified: click 'Advanced',")
    print("  then 'Go to ... (unsafe)'. It's your own app, so that's fine.")
    print("=" * 60)
    print("\nWaiting for you to finish (this window will update by itself)...")
    interval = int(r.get("interval", 5))
    deadline = time.time() + int(r.get("expires_in", 1800))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            t = post_form(TOKEN_URL, {
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "device_code": r["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
        except ApiError as e:
            if e.reason == "authorization_pending":
                continue
            if e.reason == "slow_down":
                interval += 5
                continue
            if e.reason == "access_denied":
                die("Access was refused on the Google screen. Run login again and click Allow.")
            if e.reason == "expired_token":
                die("The code expired before you finished. Run login again.")
            if e.reason == "invalid_client":
                die("Google didn't accept the client secret. Run setup again and re-paste it.")
            raise
        if not t.get("refresh_token"):
            die("Google didn't send a long-term login. Remove the app's access at "
                "https://myaccount.google.com/permissions and run login again.")
        save_json(TOKEN_PATH, {
            "refresh_token": t["refresh_token"],
            "access_token": t["access_token"],
            "expires_at": time.time() + int(t.get("expires_in", 3600)),
        })
        ch = YouTube(cfg).call("GET", "/channels", {"part": "snippet", "mine": "true"})
        names = [c["snippet"]["title"] for c in ch.get("items", [])]
        if names:
            log(f"Connected to YouTube channel: {', '.join(names)}")
        else:
            log("Connected, but this Google account has no YouTube channel. "
                "Run login again and pick the account or channel that owns your stream.")
        print("Next: python3 yt_rotate.py status\n")
        return
    die("The code expired before you finished. Run login again.")


# ---------------------------------------------------------------- YouTube lookups

def list_streams(yt):
    r = yt.call("GET", "/liveStreams", {"part": "id,snippet,cdn,status,contentDetails",
                                        "mine": "true", "maxResults": 50})
    return r.get("items", [])


def stream_key_of(s):
    return s.get("cdn", {}).get("ingestionInfo", {}).get("streamName", "")


def find_stream(yt, cfg):
    streams = list_streams(yt)
    key = (cfg.get("stream_key") or "").strip()
    if key:
        for s in streams:
            if stream_key_of(s) == key:
                return s
        die("None of your channel's stream keys matches the stream key in config.json. "
            "Run 'python3 yt_rotate.py status' to see them, then run setup again.")
    active = [s for s in streams if s.get("status", {}).get("streamStatus") == "active"]
    if len(active) == 1:
        return active[0]
    die("Couldn't tell which stream key your server uses. Run setup and paste your stream key.")


def list_broadcasts(yt, status):
    items, page = [], None
    while True:
        params = {"part": "id,snippet,contentDetails,status", "broadcastStatus": status, "maxResults": 50}
        if page:
            params["pageToken"] = page
        r = yt.call("GET", "/liveBroadcasts", params)
        items.extend(r.get("items", []))
        page = r.get("nextPageToken")
        if not page:
            return items


def bound_to(broadcasts, stream_id):
    return [b for b in broadcasts if b.get("contentDetails", {}).get("boundStreamId") == stream_id]


def age_hours(b):
    started = b.get("snippet", {}).get("actualStartTime")
    if not started:
        return 0.0
    return (utc_now() - parse_time(started)).total_seconds() / 3600


def describe(b):
    age = age_hours(b)
    age_part = f", live for {hours_text(age)}" if age else ""
    return f"\"{b['snippet'].get('title', '')}\" (https://youtu.be/{b['id']}{age_part})"


# ---------------------------------------------------------------- broadcast actions

def create_broadcast(yt, cfg):
    body = {
        "snippet": {
            "title": cfg["title"],
            "description": cfg.get("description", ""),
            "scheduledStartTime": iso(utc_now() + datetime.timedelta(minutes=1)),
        },
        "status": {
            "privacyStatus": cfg.get("privacy", "public"),
            "selfDeclaredMadeForKids": False,
        },
        "contentDetails": {
            "enableAutoStart": False,
            "enableAutoStop": False,
            "enableDvr": True,
            "recordFromStart": True,      # this is what makes YouTube save the replay
            "enableEmbed": True,
            "monitorStream": {"enableMonitorStream": False},
        },
    }
    return yt.call("POST", "/liveBroadcasts", {"part": "id,snippet,status,contentDetails"}, body)


def set_thumbnail(yt, cfg, video_id):
    path = cfg.get("thumbnail") or ""
    if not path or not os.path.exists(path):
        log("No thumbnail file found, so YouTube will pick one. (See the guide to add yours.)")
        return
    content_type = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    with open(path, "rb") as f:
        data = f.read()
    try:
        yt.upload("/thumbnails/set", {"videoId": video_id, "uploadType": "media"}, data, content_type)
        log("Thumbnail set.")
    except ApiError as e:
        log(f"Couldn't set the thumbnail ({e.reason or e.status}: {e.message}). The stream still works.")


def bind(yt, broadcast_id, stream_id):
    yt.call("POST", "/liveBroadcasts/bind",
            {"id": broadcast_id, "part": "id,contentDetails", "streamId": stream_id})


def transition(yt, broadcast_id, status):
    return yt.call("POST", "/liveBroadcasts/transition",
                   {"broadcastStatus": status, "id": broadcast_id, "part": "id,status"})


def go_live(yt, broadcast_id, attempts, wait=15):
    last = None
    for i in range(attempts):
        try:
            transition(yt, broadcast_id, "live")
            return True
        except ApiError as e:
            if e.reason == "redundantTransition":      # already live
                return True
            last = e
            if i < attempts - 1:
                log(f"  Not live yet ({e.reason or e.status}). Trying again in {wait} seconds...")
                time.sleep(wait)
    log(f"  YouTube said: {last}")
    return False


def wait_until_live(yt, broadcast_id, tries=12, wait=10):
    for _ in range(tries):
        r = yt.call("GET", "/liveBroadcasts", {"part": "status", "id": broadcast_id})
        items = r.get("items", [])
        if items and items[0]["status"].get("lifeCycleStatus") == "live":
            return True
        time.sleep(wait)
    return False


def end_broadcast(yt, b):
    try:
        transition(yt, b["id"], "complete")
        if age_hours(b) >= 12:
            log(f"Ended {describe(b)}. It ran over 12 hours, so YouTube may not save it. "
                "That's expected for this one; from now on each broadcast stays under 12.")
        else:
            log(f"Ended {describe(b)}. YouTube will save it as a public video.")
    except ApiError as e:
        if e.reason == "redundantTransition":
            return
        log(f"Couldn't end {describe(b)}: {e}")


def rotate(yt, cfg, dry_run=False):
    stream = find_stream(yt, cfg)
    sid = stream["id"]
    stream_status = stream.get("status", {}).get("streamStatus")
    if stream_status != "active":
        die(f"Your server isn't sending video to YouTube right now (stream status: {stream_status}). "
            "Check it with: systemctl status yt-stream")
    if stream.get("contentDetails", {}).get("isReusable") is False:
        die("This stream key is single-use, so new broadcasts can't share it. "
            "Use your channel's default stream key instead.")

    active = list_broadcasts(yt, "active")
    live = bound_to(active, sid)
    others = [b for b in active if b not in live]
    state = load_json(STATE_PATH, {}) or {}
    pending = None
    if state.get("pending_id"):
        upcoming = {b["id"]: b for b in list_broadcasts(yt, "upcoming")}
        pending = upcoming.get(state["pending_id"])

    if dry_run:
        log("DRY RUN - nothing will be changed.")
        if pending:
            log(f"Would reuse the broadcast an earlier run already made: {describe(pending)}")
        else:
            log(f"Would start a new public broadcast titled: \"{cfg['title']}\"")
        for b in live:
            log(f"Would then end: {describe(b)}")
        for b in others:
            log(f"Would leave alone (it uses a different stream key): {describe(b)}")
        return

    if pending:
        new = pending
        log(f"Reusing the broadcast an earlier run made: https://youtu.be/{new['id']}")
    else:
        new = create_broadcast(yt, cfg)
        state["pending_id"] = new["id"]
        save_json(STATE_PATH, state)
        log(f"Created a new broadcast: https://youtu.be/{new['id']}")
        set_thumbnail(yt, cfg, new["id"])

    if new.get("contentDetails", {}).get("boundStreamId") != sid:
        bind(yt, new["id"], sid)
        log("Connected it to your server's stream key.")

    log("Switching the new broadcast to live...")
    ok = go_live(yt, new["id"], attempts=2)
    if not ok and live:
        log("YouTube wants the old broadcast ended first. Ending it now.")
        for b in live:
            end_broadcast(yt, b)
        live = []
        ok = go_live(yt, new["id"], attempts=12)
    if not ok:
        die("The new broadcast didn't go live. The next run will try again with the same one. "
            "If this keeps happening, run 'python3 yt_rotate.py status' and send the output to Claude.")

    state.pop("pending_id", None)
    save_json(STATE_PATH, state)
    if not wait_until_live(yt, new["id"]):
        log("YouTube is still starting the new broadcast. Carrying on.")
    for b in live:
        end_broadcast(yt, b)

    # Safety check: make sure only the new broadcast is live on this stream key.
    time.sleep(30)
    for b in bound_to(list_broadcasts(yt, "active"), sid):
        if b["id"] != new["id"]:
            log("Found another live broadcast on the same stream key. Ending it so only one runs.")
            end_broadcast(yt, b)
    log(f"Done. Live now: https://youtu.be/{new['id']}")


# ---------------------------------------------------------------- commands

def cmd_status(_args):
    cfg = load_config()
    yt = YouTube(cfg)
    ch = yt.call("GET", "/channels", {"part": "snippet", "mine": "true"})
    names = [c["snippet"]["title"] for c in ch.get("items", [])]
    print(f"\nChannel: {', '.join(names) or '(none found - run login again)'}")
    key = (cfg.get("stream_key") or "").strip()
    print("\nStream keys:")
    streams = list_streams(yt)
    for s in streams:
        mark = "  <- the one in config.json" if key and stream_key_of(s) == key else ""
        reusable = s.get("contentDetails", {}).get("isReusable")
        print(f"  - {s['snippet'].get('title', '(no name)')}: key {mask(stream_key_of(s))}, "
              f"status {s['status'].get('streamStatus')}, reusable {reusable}{mark}")
    if not streams:
        print("  (none)")
    stream_ids = {s["id"]: s for s in streams}
    print("\nLive now:")
    active = list_broadcasts(yt, "active")
    for b in active:
        sid = b.get("contentDetails", {}).get("boundStreamId")
        on = stream_ids.get(sid)
        print(f"  - {describe(b)} on key {mask(stream_key_of(on)) if on else '(unknown)'}")
    if not active:
        print("  (nothing)")
    upcoming = list_broadcasts(yt, "upcoming")
    if upcoming:
        print("\nCreated but not live yet:")
        for b in upcoming:
            print(f"  - {describe(b)}")
    max_h = float(cfg.get("max_hours", DEFAULT_MAX_HOURS))
    print(f"\nA fresh broadcast starts once the live one reaches {max_h:g} hours.\n")


def cmd_rotate(args):
    cfg = load_config()
    rotate(YouTube(cfg), cfg, dry_run=args.dry_run)


def cmd_auto(args):
    cfg = load_config()
    yt = YouTube(cfg)
    max_h = float(cfg.get("max_hours", DEFAULT_MAX_HOURS))
    if max_h >= 12:
        die("max_hours in config.json must be under 12, or YouTube won't save the broadcasts.")
    stream = find_stream(yt, cfg)
    live = bound_to(list_broadcasts(yt, "active"), stream["id"])
    if live:
        oldest = max(age_hours(b) for b in live)
        if oldest < max_h:
            log(f"Live broadcast is {hours_text(oldest)} old. Fresh start at {max_h:g}h. Nothing to do.")
            return
        log(f"Live broadcast is {hours_text(oldest)} old. Starting a fresh one.")
    else:
        log("Nothing is live on this stream key. Starting a broadcast.")
    rotate(yt, cfg, dry_run=args.dry_run)


def run(cmd):
    print("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def cmd_install_timer(_args):
    if os.geteuid() != 0:
        die("This step needs admin rights. Run it as: sudo python3 yt_rotate.py install-timer")
    load_config()
    if not load_json(TOKEN_PATH):
        die("Run login first.")
    script = os.path.abspath(__file__)
    service = (
        "[Unit]\n"
        "Description=Keep each YouTube broadcast under 12 hours (yt_rotate.py)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={HERE}\n"
        f"ExecStart={sys.executable} {script} auto\n"
        "TimeoutStartSec=15min\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Check the YouTube broadcast every 30 minutes\n\n"
        "[Timer]\n"
        "OnBootSec=5min\n"
        "OnUnitActiveSec=30min\n"
        "AccuracySec=1min\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    with open(SERVICE_FILE, "w") as f:
        f.write(service)
    with open(TIMER_FILE, "w") as f:
        f.write(timer)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "yt-rotate.timer"])
    print("\nTimer is on. It checks every 30 minutes and starts a fresh broadcast when needed.")
    print("See what it did:   journalctl -u yt-rotate -n 30")
    print("See when it runs:  systemctl list-timers yt-rotate.timer\n")


def cmd_remove_timer(_args):
    if os.geteuid() != 0:
        die("This step needs admin rights. Run it as: sudo python3 yt_rotate.py remove-timer")
    subprocess.run(["systemctl", "disable", "--now", "yt-rotate.timer"])
    for path in (TIMER_FILE, SERVICE_FILE):
        if os.path.exists(path):
            os.remove(path)
    run(["systemctl", "daemon-reload"])
    print("\nTimer removed. Your stream keeps running; broadcasts just won't be refreshed any more.\n")


HINTS = {
    "quotaExceeded": "YouTube's daily API allowance is used up. It resets at midnight Pacific time; "
                     "the timer will carry on by itself after that.",
    "insufficientPermissions": "The login doesn't have permission to manage live streams. Run login again "
                               "and allow access.",
    "forbidden": "YouTube refused. Make sure you logged in with the account that owns the channel.",
    "liveStreamingNotEnabled": "Live streaming isn't enabled on the channel you logged in with.",
    "accessNotConfigured": "The YouTube Data API v3 isn't switched on in your Google Cloud project "
                           "(guide step 1).",
    "SERVICE_DISABLED": "The YouTube Data API v3 isn't switched on in your Google Cloud project "
                        "(guide step 1).",
}


def main():
    parser = argparse.ArgumentParser(description="Keep a 24/7 YouTube stream in chunks under 12 hours.")
    sub = parser.add_subparsers(dest="command")
    for name, func, helptext in [
        ("setup", cmd_setup, "ask for your settings and save them"),
        ("login", cmd_login, "connect to your YouTube channel"),
        ("status", cmd_status, "show your stream and live broadcasts"),
        ("rotate", cmd_rotate, "start a fresh broadcast right now"),
        ("auto", cmd_auto, "start a fresh broadcast only if the live one is old enough"),
        ("install-timer", cmd_install_timer, "run 'auto' every 30 minutes"),
        ("remove-timer", cmd_remove_timer, "stop the timer"),
    ]:
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(func=func)
        if name in ("rotate", "auto"):
            p.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return
    try:
        args.func(args)
    except ApiError as e:
        hint = HINTS.get(e.reason, "")
        die(f"YouTube said: {e}" + (f"\n{hint}" if hint else ""))
    except KeyboardInterrupt:
        die("Cancelled.")


if __name__ == "__main__":
    main()
