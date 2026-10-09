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
Requires: pip install -r requirements.txt   (flask yt-dlp segno)
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
# No CORS on purpose: the page is served by this same server, so no other website may call the API
# (otherwise any web page opened on a phone of the network could control the party).

@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if resp.mimetype == "text/html":
        # the page uses inline scripts/styles; the rest is locked down (no framing, no foreign
        # connections, no plugins, no <base> tricks). Media may come from Google's video servers.
        resp.headers.setdefault("Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; media-src 'self' blob: https:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    return resp

@app.errorhandler(ValueError)
@app.errorhandler(TypeError)
def _bad_input(e):
    return jsonify({"error": "Bad request"}), 400

_VID_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,20}")
_THUMB_RE  = re.compile(r"https://[A-Za-z0-9.-]*ytimg\.com/[^\s\"'<>`]*")
MAX_QUEUE  = 2000

def _clean_track(tr):
    """Whitelist + type-check a track coming from a client (never trust the browser)."""
    if not isinstance(tr, dict):
        return None
    vid = tr.get("id")
    if not isinstance(vid, str) or not _VID_ID_RE.fullmatch(vid):
        return None
    def txt(k):
        v = tr.get(k)
        return v[:300] if isinstance(v, str) else ""
    th = tr.get("thumbnail")
    if not (isinstance(th, str) and len(th) < 300 and _THUMB_RE.fullmatch(th)):
        th = "https://i.ytimg.com/vi/" + vid + "/hqdefault.jpg"
    dur = tr.get("duration")
    dur = float(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) and 0 <= dur < 1e6 else None
    return {"id": vid, "title": txt("title"), "channel": txt("channel"), "thumbnail": th, "duration": dur}

def _clean_tracks(lst):
    if not isinstance(lst, list):
        return []
    return [t for t in (_clean_track(x) for x in lst[:MAX_QUEUE]) if t]

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
    try:
        n = max(1, min(int(request.args.get("n", 20)), 50))
    except (TypeError, ValueError):
        n = 20
    if not query:
        return jsonify({"error": "Missing query"}), 400
    query = query[:200]
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
# The video is only a muted companion of the audio. It is picked from the SAME yt-dlp lookup as the
# audio (the list of formats is already in it), so showing the video costs no extra request to YouTube.
_video_cache   = {}               # video id -> (expires_at, info, born_at)
_VIDEO_TTL     = 3000             # seconds (YouTube links stay valid for hours)
_extract_locks = {}               # one lookup per track at a time; whoever else asks waits and shares it
_extract_guard = threading.Lock()

def _id_lock(vid_id):
    with _extract_guard:
        return _extract_locks.setdefault(vid_id, threading.Lock())

def _codec_rank(vcodec):
    v = vcodec or ""
    if v.startswith("avc1"):                return 0     # H.264: plays everywhere, hardware-decoded on a Pi
    if v.startswith(("vp9", "vp09")):       return 1
    if v.startswith("av01"):                return 3     # AV1: most Pi browsers can't decode it
    return 2

def pick_video_format(formats):
    """Best playable video stream out of a yt-dlp format list (or None)."""
    vids = [f for f in (formats or [])
            if f.get("url") and (f.get("vcodec") or "none") != "none"
            and f.get("protocol") in (None, "http", "https")]      # no HLS / DASH manifests: a <video> can't play them
    if not vids:
        return None
    ok = [f for f in vids if (f.get("height") or 0) <= VIDEO_MAX_HEIGHT and _codec_rank(f.get("vcodec")) < 3]
    if ok:
        return min(ok, key=lambda f: (_codec_rank(f.get("vcodec")),
                                      0 if (f.get("acodec") or "none") == "none" else 1,   # video-only is lighter
                                      -(f.get("height") or 0), -(f.get("tbr") or 0)))
    # nothing fits (only larger streams, or only AV1): take the smallest rather than nothing
    return min(vids, key=lambda f: (_codec_rank(f.get("vcodec")) >= 3, f.get("height") or 0))

def _remember_video(vid_id, info):
    if not VIDEO_MODE:
        return
    f = pick_video_format(info.get("formats"))
    if f:
        if len(_video_cache) > 50:
            _video_cache.clear()
        now = time.time()
        _video_cache[vid_id] = (now + _VIDEO_TTL, {"url": f["url"], "ext": f.get("ext"), "width": f.get("width"),
                                                   "height": f.get("height"), "vcodec": f.get("vcodec")}, now)

def get_video_info(vid_id, fresh=False):
    """Video stream for a track, or None. Normally already cached by the audio lookup of the same track.
    fresh=True (the player failed on the cached link) looks it up again, unless that was done a moment ago."""
    def cached():
        hit = _video_cache.get(vid_id)
        if hit and hit[0] > time.time() and (not fresh or time.time() - hit[2] < 10):
            return hit[1]
    got = cached()
    if got:
        return got
    with _id_lock(vid_id):                       # an audio lookup of this track may be running: wait for it
        got = cached()
        if got:
            return got
        with yt_dlp.YoutubeDL(YDL_AUDIO_OPTS) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=" + vid_id, download=False)
        _remember_video(vid_id, info)
        hit = _video_cache.get(vid_id)
        return hit[1] if hit else None

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
def _clean_err(e):
    """A short readable reason out of a yt-dlp error."""
    msg = _ANSI.sub("", str(e)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r"^\[[\w:.-]+\]\s*[\w-]{6,20}:\s*", "", msg)      # "[youtube] abc123XYZ_-: "
    return msg[:160] or e.__class__.__name__

@app.route("/video-url")
def video_url():
    if not VIDEO_MODE:
        return jsonify({"error": "Video is off: start the server with --video"}), 404
    vid_id = request.args.get("id", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid_id):
        return jsonify({"error": "Bad id"}), 400
    try:
        info = get_video_info(vid_id, fresh=request.args.get("fresh") == "1")
    except Exception as e:
        msg = _clean_err(e)
        print("[video] lookup failed for %s: %s" % (vid_id, msg))
        return jsonify({"error": msg}), 502
    if not info:
        print("[video] no playable video stream for %s" % vid_id)
        return jsonify({"error": "no playable video stream (audio only?)"}), 404
    return jsonify(info)

# ── Audio URL (normal mode) ───────────────────────────────────────────────────
@app.route("/audio-url")
def audio_url():
    vid_id = request.args.get("id", "").strip()
    if not _VID_ID_RE.fullmatch(vid_id):
        return jsonify({"error": "Bad id"}), 400
    url = "https://www.youtube.com/watch?v=" + vid_id
    with _id_lock(vid_id):
        with yt_dlp.YoutubeDL(YDL_AUDIO_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
        _remember_video(vid_id, info)          # --video: the video comes from this same lookup
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
            tr = _clean_track(b.get("track"))
            if not tr:
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

def _own_ips():
    ips = set()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except Exception:
        pass
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.connect(("10.255.255.255", 1))
        ips.add(u.getsockname()[0])
        u.close()
    except Exception:
        pass
    try:
        from urllib.parse import urlparse
        h = urlparse(PUBLIC_URL if "//" in str(PUBLIC_URL) else "//" + str(PUBLIC_URL)).hostname
        if h:
            ips.add(h)
    except Exception:
        pass
    return ips

def _is_local_client():
    """True when the request comes from the machine running the server (the Pi's own screen)."""
    ip = request.remote_addr or ""
    if ip.startswith("127.") or ip in ("::1", "::ffff:127.0.0.1"):
        return True
    return ip.replace("::ffff:", "") in _own_ips()

# ── Mode endpoint (so the front knows which mode is active) ───────────────────
@app.route("/mode")
def mode():
    return jsonify({"party": PARTY_MODE, "url": PUBLIC_URL, "pin": bool(PARTY_PIN), "video": VIDEO_MODE,
                    "local": _is_local_client()})

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
    party_lock = threading.RLock()   # re-entrant: state_snapshot() takes it again
    party_state = {
        "queue":        [],    # list of track dicts
        "current_idx":  -1,
        "is_playing":   False,
        "position":     0.0,   # seconds
        "duration":     0.0,
        "volume":       100,   # mpv volume, 0-100
        "locked":       False, # party lock (see _is_master)
        "playlists_rev": 0,    # bumped when a saved playlist changes -> clients re-fetch /playlists
        "loading":      False, # a track was chosen and mpv is not playing it yet
        "speed":        1.0,   # playback speed (hold on the video = x2)
        "open_seq":     0,     # bumped when someone starts a track by hand -> the Pi screen opens the video
    }
    party_master = {"id": None}   # never broadcast: it would let anyone impersonate the master

    mpv_proc     = None        # subprocess.Popen
    # Every play_track() call gets a number. Anything started earlier (a slow extraction, the thread
    # watching the previous mpv) sees it is out of date and stops instead of launching / advancing.
    play_gen     = {"n": 0}
    # ready: mpv's IPC socket answers, so commands reach it. Before that they would be lost silently,
    # so pause / seek are remembered and applied the moment mpv is ready (see _settle).
    mpv_state    = {"ready": False, "pending_seek": None}
    launch_lock  = threading.Lock()   # one mpv started / stopped at a time
    # A speed above 1 only lasts while the device that asked for it keeps confirming it (every few
    # seconds): if that device disappears while the finger is still down, the speed falls back to 1.
    speed_lease  = {"until": 0.0}
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
        _remember_video(vid_id, info)              # --video: the video comes from this same lookup
        formats = info.get("formats", [])
        af = [f for f in formats if f.get("vcodec") == "none" and f.get("url")]
        if not af:
            af = [f for f in formats if f.get("acodec") != "none" and f.get("url")]
        if not af:
            return None, info
        best = sorted(af, key=lambda f: f.get("abr") or 0, reverse=True)[0]
        return best["url"], info

    # ── audio URL cache (so the next track starts instantly) ─────────────────
    _audio_cache = {}              # video id -> (expires_at, (url, {"duration": ...}))

    def get_audio_info(vid_id):
        hit = _audio_cache.get(vid_id)
        if hit and hit[0] > time.time():
            return hit[1]
        with _id_lock(vid_id):     # the same track asked twice at once is looked up once (audio AND video)
            hit = _audio_cache.get(vid_id)
            if hit and hit[0] > time.time():
                return hit[1]
            url, info = extract_audio_url(vid_id)
            out = (url, {"duration": info.get("duration")})
            if url:
                if len(_audio_cache) > 50:
                    _audio_cache.clear()
                _audio_cache[vid_id] = (time.time() + 3000, out)
            return out

    # ── mpv launcher ─────────────────────────────────────────────────────────
    def _mpv_alive():
        return mpv_proc is not None and mpv_proc.poll() is None

    def _stop_mpv():
        if mpv_proc and mpv_proc.poll() is None:
            mpv_proc.terminate()
            try:
                mpv_proc.wait(timeout=3)
            except Exception:
                mpv_proc.kill()

    def play_track(idx, user=False):
        global mpv_proc
        with party_lock:
            if idx < 0 or idx >= len(party_state["queue"]):
                return                      # nothing to play there: leave everything as it is
            track = party_state["queue"][idx]
            play_gen["n"] += 1
            gen = play_gen["n"]
            party_state["current_idx"] = idx
            party_state["is_playing"]  = True
            party_state["position"]    = 0.0
            party_state["loading"]     = True
            if user:
                party_state["open_seq"] += 1
            party_state["speed"]       = 1.0           # a new track always starts at normal speed
            mpv_state["ready"]         = False
            mpv_state["pending_seek"]  = None
        broadcast(state_snapshot())         # the screens show "loading" right away

        try:
            url, info = get_audio_info(track["id"])
        except Exception:
            url, info = None, {}

        failed = False
        with party_lock:
            if gen != play_gen["n"]:
                return                      # something else was chosen while this one was loading
            if not url:
                party_state["is_playing"] = False
                party_state["loading"]    = False
                failed = True
            else:
                party_state["duration"] = info.get("duration") or track.get("duration") or 0
        if failed:
            with launch_lock:               # the previous track must not keep playing under a "stopped" icon
                with party_lock:
                    current = gen == play_gen["n"]
                if current:
                    _stop_mpv()
            broadcast(state_snapshot())
            return

        with launch_lock:
            with party_lock:
                if gen != play_gen["n"]:
                    return
            _stop_mpv()                     # (may take a moment: re-check afterwards)
            with party_lock:
                if gen != play_gen["n"]:
                    return
                vol       = party_state["volume"]
                want_play = party_state["is_playing"]
                start     = mpv_state["pending_seek"]
                mpv_state["pending_seek"] = None
            mpv_cmd = [
                "mpv",
                "--no-video",
                "--input-ipc-server=" + ipc_path,
                "--really-quiet",
                "--volume=" + str(vol),
            ]
            if not want_play:               # the user pressed pause while it was loading
                mpv_cmd.append("--pause")
            if start:                       # ...or jumped somewhere in the track
                mpv_cmd.append("--start=%s" % start)
            if AUDIO_DEVICE:
                mpv_cmd += ["--ao=alsa", "--audio-device=" + AUDIO_DEVICE]
            mpv_cmd.append(url)
            try:
                proc = subprocess.Popen(mpv_cmd)
            except Exception:
                with party_lock:
                    party_state["is_playing"] = False
                    party_state["loading"]    = False
                broadcast(state_snapshot())
                return
            mpv_proc = proc

        threading.Thread(target=_watch,  args=(proc, gen), daemon=True).start()
        threading.Thread(target=_settle, args=(proc, gen), daemon=True).start()

    def _watch(proc, gen):
        """When this mpv exits by itself, go to the next track."""
        proc.wait()
        with party_lock:
            if gen != play_gen["n"]:
                return                      # it was replaced or stopped on purpose, not a real end
            nxt      = party_state["current_idx"] + 1
            playing  = party_state["is_playing"]
            has_next = nxt < len(party_state["queue"])
        if playing and has_next:
            play_track(nxt)
        else:
            with party_lock:
                if gen == play_gen["n"]:
                    party_state["is_playing"] = False
                    party_state["loading"]    = False
                    mpv_state["ready"]        = False
            broadcast(state_snapshot())

    def _settle(proc, gen):
        """As soon as mpv answers on its IPC socket, make it match what the user asked for while it was
        loading (pause / volume / jump), then let the screens know it is playing."""
        deadline = time.time() + 10
        while time.time() < deadline and gen == play_gen["n"] and proc.poll() is None:
            if mpv_get_property("pause") is not None:
                break
            time.sleep(0.1)
        if proc.poll() is not None:
            return                          # it died: _watch deals with it
        with party_lock:
            if gen != play_gen["n"]:
                return
            want_play = party_state["is_playing"]
            seek      = mpv_state["pending_seek"]
            mpv_state["pending_seek"] = None
            mpv_send(["set_property", "pause", not want_play])
            mpv_send(["set_property", "volume", party_state["volume"]])
            mpv_send(["set_property", "speed", party_state["speed"]])
            if seek:
                mpv_send(["seek", seek, "absolute"])
            mpv_state["ready"]     = True
            party_state["loading"] = False
        broadcast(state_snapshot())
        threading.Thread(target=_warm_up, args=(gen,), daemon=True).start()

    def _warm_up(gen):
        """Music is running: look up this track's video and the NEXT track now, so the next change is instant."""
        with party_lock:
            if gen != play_gen["n"]:
                return
            q, i = party_state["queue"], party_state["current_idx"]
            cur = q[i]["id"]     if 0 <= i < len(q)     else None
            nxt = q[i + 1]["id"] if 0 <= i + 1 < len(q) else None
        jobs = []
        if VIDEO_MODE and cur: jobs.append((get_video_info, cur))
        if nxt:                jobs.append((get_audio_info, nxt))
        if VIDEO_MODE and nxt: jobs.append((get_video_info, nxt))
        for fn, vid in jobs:
            if gen != play_gen["n"]:
                return
            try:
                fn(vid)
            except Exception as e:
                print("[warm-up] %s %s: %s" % (fn.__name__, vid, _clean_err(e)))

    # ── Position poller ───────────────────────────────────────────────────────
    def _position_poller():
        while True:
            time.sleep(1)
            if not mpv_state["ready"]:
                continue                    # still loading: nothing to read
            speed_reset = False
            with party_lock:
                want_play = party_state["is_playing"]
                if party_state["speed"] != 1.0 and time.time() > speed_lease["until"]:
                    party_state["speed"] = 1.0           # the device that asked for it is gone
                    mpv_send(["set_property", "speed", 1.0])
                    speed_reset = True
            if speed_reset:
                broadcast(state_snapshot())
            paused = mpv_get_property("pause")
            if paused is not None and bool(paused) == want_play:
                # mpv disagrees with the icon (a command got lost, or something else toggled it): mpv follows the UI
                mpv_send(["set_property", "pause", not want_play])
            if want_play:
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
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Bad request"}), 400
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
            track = _clean_track(data.get("track"))
            if not track:
                return jsonify({"error": "Missing track"}), 400
            with party_lock:
                # "Play now": the track goes right after the current one, and plays immediately
                ins = party_state["current_idx"] + 1 if party_state["current_idx"] >= 0 else 0
                ins = min(ins, len(party_state["queue"]))
                party_state["queue"].insert(ins, track)
            threading.Thread(target=play_track, args=(ins,), kwargs={"user": True}, daemon=True).start()

        elif cmd == "add_next":
            track = _clean_track(data.get("track"))
            if not track:
                return jsonify({"error": "Bad track"}), 400
            with party_lock:
                ins = party_state["current_idx"] + 1 if party_state["current_idx"] >= 0 else len(party_state["queue"])
                party_state["queue"].insert(ins, track)
            broadcast(state_snapshot())

        elif cmd == "add_end":
            track = _clean_track(data.get("track"))
            if not track:
                return jsonify({"error": "Bad track"}), 400
            with party_lock:
                if len(party_state["queue"]) >= MAX_QUEUE:
                    return jsonify({"error": "Queue is full"}), 400
                party_state["queue"].append(track)
            broadcast(state_snapshot())

        elif cmd == "play_list":
            # Replace the whole queue with these tracks and start from the first one
            tracks = _clean_tracks(data.get("tracks"))
            if not tracks:
                return jsonify({"error": "Missing tracks"}), 400
            with party_lock:
                party_state["queue"] = list(tracks)
                party_state["current_idx"] = -1
            threading.Thread(target=play_track, args=(0,), kwargs={"user": True}, daemon=True).start()

        elif cmd == "add_many":
            tracks = _clean_tracks(data.get("tracks"))
            with party_lock:
                party_state["queue"].extend(tracks[:max(0, MAX_QUEUE - len(party_state["queue"]))])
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

        elif cmd == "speed":
            try:
                v = max(1.0, min(2.0, float(data.get("value", 1))))
            except (TypeError, ValueError):
                return jsonify({"error": "Bad speed"}), 400
            with party_lock:
                party_state["speed"] = v
                speed_lease["until"] = time.time() + 15 if v != 1.0 else 0.0
                mpv_send(["set_property", "speed", v])   # lost if mpv isn't ready yet: _settle applies it
            broadcast(state_snapshot())

        elif cmd == "pause_toggle":
            start_idx = None
            with party_lock:
                if not _mpv_alive() and not party_state["loading"]:
                    # nothing is playing (queue finished / cleared): "play" must start something,
                    # not just flip the icon
                    q, cur = party_state["queue"], party_state["current_idx"]
                    if q:
                        start_idx = cur if 0 <= cur < len(q) else 0
                else:
                    party_state["is_playing"] = not party_state["is_playing"]
                    # an explicit value (never "cycle"): it can't drift out of step with the icon.
                    # If mpv isn't ready yet this is lost, and _settle applies it once it is.
                    mpv_send(["set_property", "pause", not party_state["is_playing"]])
            if start_idx is not None:
                threading.Thread(target=play_track, args=(start_idx,), kwargs={"user": True}, daemon=True).start()
            else:
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
            pos = max(0.0, float(data.get("position", 0)))
            if pos != pos or pos > 1e7:             # NaN / absurd values
                return jsonify({"error": "Bad position"}), 400
            with party_lock:
                party_state["position"] = pos
                if mpv_state["ready"]:
                    mpv_send(["seek", pos, "absolute"])
                else:
                    mpv_state["pending_seek"] = pos     # still loading: applied when mpv starts
            broadcast(state_snapshot())

        elif cmd == "play_idx":
            idx = int(data.get("idx", 0))
            threading.Thread(target=play_track, args=(idx,), kwargs={"user": True}, daemon=True).start()

        elif cmd == "remove":
            idx = int(data.get("idx", 0))
            with party_lock:
                if 0 <= idx < len(party_state["queue"]):
                    party_state["queue"].pop(idx)
                    if party_state["current_idx"] == idx:
                        party_state["current_idx"] = -1
                        party_state["is_playing"] = False
                        party_state["loading"] = False
                        play_gen["n"] += 1              # cancels a load that is still in progress
                        mpv_state["ready"] = False
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
                party_state["loading"] = False
                play_gen["n"] += 1                      # cancels a load that is still in progress
                mpv_state["ready"] = False
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
