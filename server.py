#!/usr/bin/env python3
"""
Lightweight YouTube audio server for Raspberry Pi
Requires: pip install flask yt-dlp
"""

from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import yt_dlp
import json

app = Flask(__name__)
CORS(app)

YDL_SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "default_search": "ytsearch",
    "skip_download": True,
}

YDL_AUDIO_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio",
}


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    n = min(int(request.args.get("n", 20)), 50)
    if not query:
        return jsonify({"error": "Missing query"}), 400

    opts = {**YDL_SEARCH_OPTS}
    with yt_dlp.YoutubeDL(opts) as ydl:
        results = ydl.extract_info(f"ytsearch{n}:{query}", download=False)

    entries = results.get("entries", [])
    videos = []
    for e in entries:
        if not e:
            continue
        vid_id = e.get("id") or e.get("url", "").split("?v=")[-1]
        videos.append({
            "id": vid_id,
            "title": e.get("title", ""),
            "channel": e.get("uploader") or e.get("channel", ""),
            "duration": e.get("duration"),
            "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
            "views": e.get("view_count"),
        })

    return jsonify({"results": videos})


@app.route("/audio-url")
def audio_url():
    vid_id = request.args.get("id", "").strip()
    if not vid_id:
        return jsonify({"error": "Missing id"}), 400

    url = f"https://www.youtube.com/watch?v={vid_id}"
    with yt_dlp.YoutubeDL(YDL_AUDIO_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])
    # pick best audio-only format
    audio_formats = [
        f for f in formats
        if f.get("vcodec") == "none" and f.get("url")
    ]
    if not audio_formats:
        # fallback: any format with audio
        audio_formats = [f for f in formats if f.get("acodec") != "none" and f.get("url")]

    if not audio_formats:
        return jsonify({"error": "No audio stream found"}), 404

    best = sorted(audio_formats, key=lambda f: f.get("abr") or 0, reverse=True)[0]

    return jsonify({
        "url": best["url"],
        "ext": best.get("ext", "webm"),
        "abr": best.get("abr"),
        "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
        "title": info.get("title", ""),
        "channel": info.get("uploader") or info.get("channel", ""),
        "duration": info.get("duration"),
    })


if __name__ == "__main__":
    print("🎵 PiTube backend running on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
