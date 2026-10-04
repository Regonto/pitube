# PiTube

Lightweight YouTube audio player for Raspberry Pi.
Static HTML front-end + Flask/yt-dlp backend. No video decoding — audio only.

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo apt install ffmpeg mpv
```

## Usage

### Normal mode
Each client plays audio locally in their browser.
```bash
python3 server.py
```

### Party mode
Audio plays on the Pi via mpv. All connected clients share the same queue and state in real time.
```bash
python3 server.py --party
```

Open `http://<pi-ip>:5000` from any device on the network.

## Notes

- Keep yt-dlp up to date or audio extraction will break: `pip install -U yt-dlp`
- Party mode requires `mpv` installed on the Pi
- Saved playlists are stored in `playlists.json` next to `server.py`
