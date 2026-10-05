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

### QR code
In party mode only, a QR code pointing to the server address is shown on the left of the results.
Long-click (or right-click) the logo to hide / show it. On narrow screens it opens as a pop-up instead.

The address defaults to `192.168.1.42:5000`. Change it with:
```bash
PITUBE_URL=192.168.1.50:5000 python3 server.py --party
# or
python3 server.py --party --url=192.168.1.50:5000
```

Logos are read from `assets/logo.png` (normal) and `assets/logo_party.png` (party).

## Notes

- Keep yt-dlp up to date or audio extraction will break: `pip install -U yt-dlp`
- Party mode requires `mpv` installed on the Pi
- Saved playlists are stored in `playlists.json` next to `server.py`
