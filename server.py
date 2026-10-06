#!/usr/bin/env python3
"""
PiTube backend
Normal mode : python3 server.py
Party mode  : python3 server.py --party
  -> audio plays via mpv on the Pi, all clients share the same state
Party + PIN : python3 server.py --party -pin 1234
  -> locking / unlocking the party then requires this 4-digit code
Video       : add --video to any of the above (python3 server.py --party --video)
  -> a button in the player shows the video (audio still comes from mpv / the browser)
Requires: pip install -r requirements.txt   (flask flask-cors yt-dlp segno)
System:   sudo apt install mpv
"""

import sys
import os
import io
import re
import hmac
import json
import time
import queue
import socket
import struct
import threading
import subprocess

from flask import Flask, jsonify, request, Response, send_from_directory
from flask_cors import CORS
import yt_dlp

# ── Mode ─────────────────────────────────────────────────────────────────────
PARTY_MODE = "--party" in sys.argv

# --video: also serve the video stream, so the page can show it (works with or without --party)
VIDEO_MODE = "--video" in sys.argv
try:
    VIDEO_MAX_HEIGHT = max(144, min(2160, int(os.environ.get("PITUBE_VIDEO_HEIGHT", "720"))))
except ValueError:
    VIDEO_MAX_HEIGHT = 720

# ── Public address (encoded in the QR code) ──────────────────────────────────
# Phones can't use "localhost", so the QR code must carry the Pi's LAN address.
# Default: 192.168.1.42:5000. Override with  PITUBE_URL=192.168.1.50:5000  or  --url=192.168.1.50:5000
def _public_url():
    url = os.environ.get("PITUBE_URL", "")
    for a in sys.argv[1:]:
        if a.startswith("--url="):
            url = a[len("--url="):]
    url = (url or "192.168.1.42:5000").strip()
    if "://" not in url:
        url = "http://" + url
    return url

PUBLIC_URL = _public_url()

# ── Party PIN (optional, party mode only) ─────────────────────────────────────
# python3 server.py --party -pin 1234     (also: --pin 1234, -pin=1234, --pin=1234)
# When set, locking and unlocking the party both require this 4-digit code.
def _parse_pin():
    argv, pin, given = sys.argv[1:], "", False
    for i, a in enumerate(argv):
        if a in ("-pin", "--pin"):
            given = True
            pin = argv[i + 1] if i + 1 < len(argv) else ""
        elif a.startswith(("-pin=", "--pin=")):
            given = True
            pin = a.split("=", 1)[1]
    if not given:
        return None
    if not PARTY_MODE:
        print("Warning: -pin is ignored, it only works with --party")
        return None
    if not re.fullmatch(r"[0-9]{4}", pin):
        sys.exit("Error: -pin needs exactly 4 digits, e.g.  python3 server.py --party -pin 1234")
    return pin

PARTY_PIN = _parse_pin()

# Brute-force protection: 5 wrong codes from one address -> locked out for 60 s
PIN_MAX_FAILS, PIN_LOCKOUT = 5, 60
_pin_fails = {}   # ip -> [wrong attempts, locked-out until (epoch)]

def _check_pin(data):
    """None if the PIN is not required or correct, else a (response, status) to return."""
    if not PARTY_PIN:
        return None
    ip = request.remote_addr
    rec = _pin_fails.get(ip, [0, 0.0])
    now = time.time()
    if now < rec[1]:
        return jsonify({"error": "too_many_attempts", "retry_in": int(rec[1] - now) + 1}), 429
    given = str((data or {}).get("pin", ""))
    if hmac.compare_digest(given.encode(), PARTY_PIN.encode()):
        _pin_fails.pop(ip, None)
        return None
    rec[0] += 1
    if rec[0] >= PIN_MAX_FAILS:
        rec = [0, now + PIN_LOCKOUT]
    _pin_fails[ip] = rec
    return jsonify({"error": "bad_pin"}), 403

# ── Party lock ────────────────────────────────────────────────────────────────
# Anyone can lock the party; the one who did becomes the "master" (identified by the
# random id their browser sends in the X-Client-Id header) and is the only one who can
# unlock or use the restricted actions. The Pi itself (localhost) is always master,
# so a locked party can never get stuck. Everybody else may still:
#   - add tracks to the END of the queue
#   - add tracks to a saved playlist (additions only)
LOCK_ALLOWED_CMDS = {"add_end", "add_many"}

def _is_master():
    if request.remote_addr in ("127.0.0.1", "::1"):
        return True
    cid = request.headers.get("X-Client-Id", "")
    return bool(cid) and cid == party_master["id"]

def _guest_locked():
    """True when the party is locked and this request does NOT come from the master."""
    return PARTY_MODE and party_state["locked"] and not _is_master()

app = Flask(__name__)
CORS(app)

PLAYLISTS_FILE = os.path.join(os.path.dirname(__file__), "playlists.json")

# Party mode audio output (mpv).
# Default: send audio straight to the HDMI port through ALSA (no PipeWire needed).
# Override with e.g.  PITUBE_AUDIO_DEVICE="alsa/hdmi:CARD=vc4hdmi,DEV=1"
# Set PITUBE_AUDIO_DEVICE="" to let mpv pick the system default output.
# List available devices with:  mpv --audio-device=help
AUDIO_DEVICE = os.environ.get("PITUBE_AUDIO_DEVICE", "alsa/hdmi:CARD=vc4hdmi,DEV=0")

YDL_SEARCH_OPTS = {
    "quiet": True, "no_warnings": True,
    "extract_flat": True, "default_search": "ytsearch", "skip_download": True,
}
YDL_AUDIO_OPTS = {
    "quiet": True, "no_warnings": True,
    "skip_download": True,
    "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio",
}

# ── Saved playlists ───────────────────────────────────────────────────────────
def load_playlists():
    if not os.path.exists(PLAYLISTS_FILE):
        return {}
    with open(PLAYLISTS_FILE) as f:
        return json.load(f)

def save_playlists(data):
    tmp = PLAYLISTS_FILE + ".tmp"          # write then rename: a crash can't leave a half-written file
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PLAYLISTS_FILE)

playlists_lock = threading.Lock()
playlists_rev  = {"n": 0}                  # bumped on every change so other devices know to refresh
_PL_TRACK_KEYS = ("id", "title", "channel", "thumbnail", "duration")

# ── Search ────────────────────────────────────────────────────────────────────
@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    n = min(int(request.args.get("n", 20)), 50)
    if not query:
        return jsonify({"error": "Missing query"}), 400
    with yt_dlp.YoutubeDL(YDL_SEARCH_OPTS.copy()) as ydl:
        results = ydl.extract_info("ytsearch" + str(n) + ":" + query, download=False)
    videos = []
    for e in (results.get("entries") or []):
        if not e:
            continue
        vid_id = e.get("id") or e.get("url", "").split("?v=")[-1]
        videos.append({
            "id": vid_id,
            "title": e.get("title", ""),
            "channel": e.get("uploader") or e.get("channel", ""),
            "duration": e.get("duration"),
            "thumbnail": "https://i.ytimg.com/vi/" + vid_id + "/mqdefault.jpg",
            "views": e.get("view_count"),
        })
    return jsonify({"results": videos})

# ── Video URL (--video) ───────────────────────────────────────────────────────
# The video is only a muted companion of the audio, so we want a video-only stream.
# H.264 first (hardware-decoded on a Pi), capped at PITUBE_VIDEO_HEIGHT (default 720).
_h = VIDEO_MAX_HEIGHT
YDL_VIDEO_OPTS = {
    "quiet": True, "no_warnings": True, "skip_download": True,
    "format": ("bestvideo[height<={h}][vcodec^=avc1]/bestvideo[height<={h}][ext=mp4]/"
               "bestvideo[height<={h}]/best[height<={h}]/best").format(h=_h),
}
_video_cache = {}                 # video id -> (expires_at, info)
_video_lock  = threading.Lock()   # one extraction at a time; guests asking for the same track share it
_VIDEO_TTL   = 3000               # seconds (YouTube links stay valid for hours)

def get_video_info(vid_id):
    """Cached yt-dlp lookup of a video-only stream. Returns a dict, or None if there is none."""
    hit = _video_cache.get(vid_id)
    if hit and hit[0] > time.time():
        return hit[1]
    with _video_lock:
        hit = _video_cache.get(vid_id)
        if hit and hit[0] > time.time():
            return hit[1]
        with yt_dlp.YoutubeDL(YDL_VIDEO_OPTS) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=" + vid_id, download=False)
        f = (info.get("requested_formats") or [info])[0]
        if not f.get("url"):
            return None
        out = {"url": f["url"], "ext": f.get("ext"), "width": f.get("width"),
               "height": f.get("height"), "vcodec": f.get("vcodec")}
        if len(_video_cache) > 50:
            _video_cache.clear()
        _video_cache[vid_id] = (time.time() + _VIDEO_TTL, out)
        return out

@app.route("/video-url")
def video_url():
    if not VIDEO_MODE:
        return jsonify({"error": "Video is off: start the server with --video"}), 404
    vid_id = request.args.get("id", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid_id):
        return jsonify({"error": "Bad id"}), 400
    try:
        info = get_video_info(vid_id)
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 502
    if not info:
        return jsonify({"error": "No video stream found"}), 404
    return jsonify(info)

# ── Audio URL (normal mode) ───────────────────────────────────────────────────
@app.route("/audio-url")
def audio_url():
    vid_id = request.args.get("id", "").strip()
    if not vid_id:
        return jsonify({"error": "Missing id"}), 400
    url = "https://www.youtube.com/watch?v=" + vid_id
    with yt_dlp.YoutubeDL(YDL_AUDIO_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = info.get("formats", [])
    af = [f for f in formats if f.get("vcodec") == "none" and f.get("url")]
    if not af:
        af = [f for f in formats if f.get("acodec") != "none" and f.get("url")]
    if not af:
        return jsonify({"error": "No audio stream found"}), 404
    best = sorted(af, key=lambda f: f.get("abr") or 0, reverse=True)[0]
    return jsonify({
        "url": best["url"], "ext": best.get("ext", "webm"), "abr": best.get("abr"),
        "thumbnail": "https://i.ytimg.com/vi/" + vid_id + "/hqdefault.jpg",
        "title": info.get("title", ""),
        "channel": info.get("uploader") or info.get("channel", ""),
        "duration": info.get("duration"),
    })

# ── Saved playlists CRUD ──────────────────────────────────────────────────────
@app.route("/playlists", methods=["GET"])
def get_playlists():
    return jsonify(load_playlists())

# Saved playlists are modified ONE operation at a time, applied on the server to the current
# file. (Sending a whole copy from each device made devices overwrite each other's additions.)
#   {op: "create", name} | {op: "delete", name}
#   {op: "add_track", name, track} | {op: "remove_track", name, id}
# While the party is locked, guests may only create playlists and add tracks.
@app.route("/playlists/op", methods=["POST"])
def playlists_op():
    b = request.get_json(silent=True) or {}
    op, name = b.get("op"), b.get("name")
    if op not in ("create", "delete", "add_track", "remove_track"):
        return jsonify({"error": "Unknown op"}), 400
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        return jsonify({"error": "Bad name"}), 400
    name = name.strip()
    if _guest_locked() and op not in ("create", "add_track"):
        return jsonify({"error": "locked"}), 403

    with playlists_lock:
        data = load_playlists()
        if op == "create":
            data.setdefault(name, [])
        elif op == "delete":
            data.pop(name, None)
        elif op == "add_track":
            tr = b.get("track")
            if not isinstance(tr, dict) or not isinstance(tr.get("id"), str) or not tr["id"]:
                return jsonify({"error": "Bad track"}), 400
            if name not in data:
                return jsonify({"error": "no_such_playlist"}), 404
            if not any(t.get("id") == tr["id"] for t in data[name]):
                data[name].append({k: tr.get(k) for k in _PL_TRACK_KEYS})
        else:  # remove_track
            tid = b.get("id")
            if not isinstance(tid, str):
                return jsonify({"error": "Bad id"}), 400
            if name in data:
                data[name] = [t for t in data[name] if t.get("id") != tid]
        save_playlists(data)
        playlists_rev["n"] += 1
        rev = playlists_rev["n"]

    if PARTY_MODE:                      # tell every connected device (incl. the master) to refresh
        with party_lock:
            party_state["playlists_rev"] = rev
        broadcast(state_snapshot())
    return jsonify({"ok": True, "playlists": data})

# ── Serve front-end ───────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(__file__)

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "pitube.html")

@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(os.path.join(BASE_DIR, "assets"), filename)

# ── Mode endpoint (so the front knows which mode is active) ───────────────────
@app.route("/mode")
def mode():
    return jsonify({"party": PARTY_MODE, "url": PUBLIC_URL, "pin": bool(PARTY_PIN), "video": VIDEO_MODE})

# ── QR code pointing to the server address (needs: pip install segno) ─────────
@app.route("/qr.svg")
def qr_svg():
    try:
        import segno
    except ImportError:
        return Response("QR unavailable: pip install segno", status=501, mimetype="text/plain")
    buf = io.BytesIO()
    segno.make(PUBLIC_URL, error="m").save(
        buf, kind="svg", scale=8, border=3, dark="#000", light="#fff",
        omitsize=True, xmldecl=False)
    return Response(buf.getvalue(), mimetype="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})

# =============================================================================
# PARTY MODE
# =============================================================================
if PARTY_MODE:
    # ── Shared state ─────────────────────────────────────────────────────────
    party_lock = threading.Lock()
    party_state = {
        "queue":        [],    # list of track dicts
        "current_idx":  -1,
        "is_playing":   False,
        "position":     0.0,   # seconds
        "duration":     0.0,
        "volume":       100,   # mpv volume, 0-100
        "locked":       False, # party lock (see _is_master)
        "playlists_rev": 0,    # bumped when a saved playlist changes -> clients re-fetch /playlists
    }
    party_master = {"id": None}   # never broadcast: it would let anyone impersonate the master

    mpv_proc     = None        # subprocess.Popen
    ipc_path     = "/tmp/pitube-mpv.sock"
    sse_clients  = []          # list of queue.Queue

    # ── SSE broadcast ────────────────────────────────────────────────────────
    def broadcast(state):
        msg = "data: " + json.dumps(state) + "\n\n"
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)

    def state_snapshot():
        with party_lock:
            return dict(party_state)

    # ── mpv IPC helper ───────────────────────────────────────────────────────
    def mpv_send(cmd):
        """Send a JSON command to mpv via IPC socket. Fire-and-forget."""
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(ipc_path)
            s.sendall((json.dumps({"command": cmd}) + "\n").encode())
            s.close()
        except Exception:
            pass

    def mpv_get_property(prop):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(ipc_path)
            req = json.dumps({"command": ["get_property", prop], "request_id": 1}) + "\n"
            s.sendall(req.encode())
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            s.close()
            resp = json.loads(data.split(b"\n")[0])
            return resp.get("data")
        except Exception:
            return None

    # ── audio extraction ─────────────────────────────────────────────────────
    def extract_audio_url(vid_id):
        url = "https://www.youtube.com/watch?v=" + vid_id
        with yt_dlp.YoutubeDL(YDL_AUDIO_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
        formats = info.get("formats", [])
        af = [f for f in formats if f.get("vcodec") == "none" and f.get("url")]
        if not af:
            af = [f for f in formats if f.get("acodec") != "none" and f.get("url")]
        if not af:
            return None, info
        best = sorted(af, key=lambda f: f.get("abr") or 0, reverse=True)[0]
        return best["url"], info

    # ── mpv launcher ─────────────────────────────────────────────────────────
    def play_track(idx):
        global mpv_proc
        with party_lock:
            if idx < 0 or idx >= len(party_state["queue"]):
                party_state["is_playing"] = False
                broadcast(state_snapshot())
                return
            track = party_state["queue"][idx]
            party_state["current_idx"] = idx
            party_state["is_playing"]  = True
            party_state["position"]    = 0.0

        # Extract URL in background so we don't block the lock
        audio_url_str, info = extract_audio_url(track["id"])
        if not audio_url_str:
            with party_lock:
                party_state["is_playing"] = False
            broadcast(state_snapshot())
            return

        # Update duration from yt-dlp info
        with party_lock:
            party_state["duration"] = info.get("duration") or track.get("duration") or 0

        # --video: look the video stream up now, so it is cached when the devices ask for it
        if VIDEO_MODE:
            def _prefetch_video(vid):
                try:
                    get_video_info(vid)
                except Exception:
                    pass
            threading.Thread(target=_prefetch_video, args=(track["id"],), daemon=True).start()

        # Kill previous mpv
        if mpv_proc and mpv_proc.poll() is None:
            mpv_proc.terminate()
            try:
                mpv_proc.wait(timeout=3)
            except Exception:
                mpv_proc.kill()

        with party_lock:
            vol = party_state["volume"]
        mpv_cmd = [
            "mpv",
            "--no-video",
            "--input-ipc-server=" + ipc_path,
            "--really-quiet",
            "--volume=" + str(vol),
        ]
        if AUDIO_DEVICE:
            mpv_cmd += ["--ao=alsa", "--audio-device=" + AUDIO_DEVICE]
        mpv_cmd.append(audio_url_str)
        mpv_proc = subprocess.Popen(mpv_cmd)

        broadcast(state_snapshot())

        # Wait for mpv to finish, then auto-advance
        def _wait():
            mpv_proc.wait()
            with party_lock:
                cur = party_state["current_idx"]
                nxt = cur + 1
                playing = party_state["is_playing"]
            if playing and nxt < len(party_state["queue"]):
                play_track(nxt)
            else:
                with party_lock:
                    party_state["is_playing"] = False
                broadcast(state_snapshot())

        threading.Thread(target=_wait, daemon=True).start()

    # ── Position poller ───────────────────────────────────────────────────────
    def _position_poller():
        while True:
            time.sleep(1)
            if party_state["is_playing"]:
                pos = mpv_get_property("time-pos")
                if pos is not None:
                    with party_lock:
                        party_state["position"] = float(pos)
                    broadcast(state_snapshot())

    threading.Thread(target=_position_poller, daemon=True).start()

    # ── SSE endpoint ──────────────────────────────────────────────────────────
    @app.route("/stream")
    def stream():
        q = queue.Queue()
        sse_clients.append(q)
        # send current state immediately
        q.put("data: " + json.dumps(state_snapshot()) + "\n\n")

        def generate():
            try:
                while True:
                    try:
                        msg = q.get(timeout=30)
                        yield msg
                    except queue.Empty:
                        yield ": heartbeat\n\n"
            except GeneratorExit:
                if q in sse_clients:
                    sse_clients.remove(q)

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ── Command endpoint ───────────────────────────────────────────────────────
    @app.route("/command", methods=["POST"])
    def command():
        data = request.get_json()
        cmd  = data.get("cmd")

        # ── party lock ──
        if cmd == "lock":
            cid = request.headers.get("X-Client-Id", "")
            if not cid:
                return jsonify({"error": "Missing client id"}), 400
            err = _check_pin(data)
            if err:
                return err
            with party_lock:
                # without a PIN only the master may re-lock; with the right PIN anyone may take over
                if party_state["locked"] and not PARTY_PIN and not _is_master():
                    return jsonify({"error": "locked"}), 403
                party_state["locked"] = True
                party_master["id"] = cid
            broadcast(state_snapshot())
            return jsonify({"ok": True})

        if cmd == "unlock":
            if PARTY_PIN:
                err = _check_pin(data)       # the code replaces the master check
                if err:
                    return err
            elif not _is_master():
                return jsonify({"error": "locked"}), 403
            with party_lock:
                party_state["locked"] = False
                party_master["id"] = None
            broadcast(state_snapshot())
            return jsonify({"ok": True})

        if party_state["locked"] and cmd not in LOCK_ALLOWED_CMDS and not _is_master():
            return jsonify({"error": "locked"}), 403

        if cmd == "play":
            track = data.get("track")
            if not track:
                return jsonify({"error": "Missing track"}), 400
            with party_lock:
                party_state["queue"].insert(0, track)
                if party_state["current_idx"] >= 0:
                    party_state["current_idx"] += 1
            threading.Thread(target=play_track, args=(0,), daemon=True).start()

        elif cmd == "add_next":
            track = data.get("track")
            with party_lock:
                ins = party_state["current_idx"] + 1 if party_state["current_idx"] >= 0 else len(party_state["queue"])
                party_state["queue"].insert(ins, track)
            broadcast(state_snapshot())

        elif cmd == "add_end":
            track = data.get("track")
            with party_lock:
                party_state["queue"].append(track)
            broadcast(state_snapshot())

        elif cmd == "play_list":
            # Replace the whole queue with these tracks and start from the first one
            tracks = data.get("tracks") or []
            if not tracks:
                return jsonify({"error": "Missing tracks"}), 400
            with party_lock:
                party_state["queue"] = list(tracks)
                party_state["current_idx"] = -1
            threading.Thread(target=play_track, args=(0,), daemon=True).start()

        elif cmd == "add_many":
            tracks = data.get("tracks") or []
            with party_lock:
                party_state["queue"].extend(tracks)
            broadcast(state_snapshot())

        elif cmd == "volume":
            try:
                vol = max(0, min(100, int(float(data.get("value", 100)))))
            except (TypeError, ValueError):
                return jsonify({"error": "Bad volume"}), 400
            with party_lock:
                party_state["volume"] = vol
            mpv_send(["set_property", "volume", vol])
            broadcast(state_snapshot())

        elif cmd == "pause_toggle":
            with party_lock:
                party_state["is_playing"] = not party_state["is_playing"]
            mpv_send(["cycle", "pause"])
            broadcast(state_snapshot())

        elif cmd == "next":
            with party_lock:
                nxt = party_state["current_idx"] + 1
            threading.Thread(target=play_track, args=(nxt,), daemon=True).start()

        elif cmd == "prev":
            with party_lock:
                prv = max(0, party_state["current_idx"] - 1)
            threading.Thread(target=play_track, args=(prv,), daemon=True).start()

        elif cmd == "seek":
            pos = float(data.get("position", 0))
            mpv_send(["seek", pos, "absolute"])
            with party_lock:
                party_state["position"] = pos
            broadcast(state_snapshot())

        elif cmd == "play_idx":
            idx = int(data.get("idx", 0))
            threading.Thread(target=play_track, args=(idx,), daemon=True).start()

        elif cmd == "remove":
            idx = int(data.get("idx", 0))
            with party_lock:
                if 0 <= idx < len(party_state["queue"]):
                    party_state["queue"].pop(idx)
                    if party_state["current_idx"] == idx:
                        party_state["current_idx"] = -1
                        party_state["is_playing"] = False
                        if mpv_proc and mpv_proc.poll() is None:
                            mpv_proc.terminate()
                    elif party_state["current_idx"] > idx:
                        party_state["current_idx"] -= 1
            broadcast(state_snapshot())

        elif cmd == "reorder":
            frm = int(data.get("from", 0))
            to  = int(data.get("to", 0))
            with party_lock:
                q = party_state["queue"]
                if 0 <= frm < len(q) and 0 <= to < len(q):
                    item = q.pop(frm)
                    q.insert(to, item)
                    cur = party_state["current_idx"]
                    if cur == frm:
                        party_state["current_idx"] = to
                    elif frm < cur <= to:
                        party_state["current_idx"] -= 1
                    elif to <= cur < frm:
                        party_state["current_idx"] += 1
            broadcast(state_snapshot())

        elif cmd == "clear":
            with party_lock:
                party_state["queue"] = []
                party_state["current_idx"] = -1
                party_state["is_playing"] = False
            if mpv_proc and mpv_proc.poll() is None:
                mpv_proc.terminate()
            broadcast(state_snapshot())

        return jsonify({"ok": True})

    @app.route("/lock")
    def lock_status():
        return jsonify({"locked": party_state["locked"], "master": _is_master()})

    @app.route("/party-state")
    def party_state_route():
        return jsonify(state_snapshot())

# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    mode_str = " [PARTY MODE]" if PARTY_MODE else ""
    print("PiTube backend running on http://0.0.0.0:5000" + mode_str)
    print("QR code points to " + PUBLIC_URL)
    if VIDEO_MODE:
        print("Video: on (max %dp)" % VIDEO_MAX_HEIGHT)
    if PARTY_PIN:
        print("Party lock PIN: enabled")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
