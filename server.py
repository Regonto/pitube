#!/usr/bin/env python3
"""
PiTube backend
Normal mode : python3 server.py
Party mode  : python3 server.py --party
  -> audio plays via mpv on the Pi, all clients share the same state
Requires: pip install flask flask-cors yt-dlp
System:   sudo apt install mpv
"""

import sys
import os
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

app = Flask(__name__)
CORS(app)

PLAYLISTS_FILE = os.path.join(os.path.dirname(__file__), "playlists.json")

YDL_SEARCH_OPTS = {
    "quiet": True, "no_warnings": True,
    "extract_flat": True, "default_search": "ytsearch", "skip_download": True,
}
YDL_AUDIO_OPTS = {
    "quiet": True, "no_warnings": True,
    "skip_download": True,
    "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio",
}

@app.route('/')
def index():
    return send_from_directory('.', 'pitube.html')

# ── Saved playlists ───────────────────────────────────────────────────────────
def load_playlists():
    if not os.path.exists(PLAYLISTS_FILE):
        return {}
    with open(PLAYLISTS_FILE) as f:
        return json.load(f)

def save_playlists(data):
    with open(PLAYLISTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

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

@app.route("/playlists", methods=["POST"])
def save_playlists_route():
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid data"}), 400
    save_playlists(data)
    return jsonify({"ok": True})

# ── Mode endpoint (so the front knows which mode is active) ───────────────────
@app.route("/mode")
def mode():
    return jsonify({"party": PARTY_MODE})

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
    }

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

        # Kill previous mpv
        if mpv_proc and mpv_proc.poll() is None:
            mpv_proc.terminate()
            try:
                mpv_proc.wait(timeout=3)
            except Exception:
                mpv_proc.kill()

        mpv_proc = subprocess.Popen([
            "mpv",
            "--no-video",
            "--input-ipc-server=" + ipc_path,
            "--really-quiet",
            audio_url_str,
        ])

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

    @app.route("/party-state")
    def party_state_route():
        return jsonify(state_snapshot())

# =============================================================================
# MAIN
# =============================================================================
if __name__ == "__main__":
    mode_str = " [PARTY MODE]" if PARTY_MODE else ""
    print("PiTube backend running on http://0.0.0.0:5000" + mode_str)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
