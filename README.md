# PiTube

Very light Youtube front solution for low-specs Raspberry Pi
Front HTML statique + backend Flask/yt-dlp. Pas de vidéo, juste l'audio — idéal pour les petites configs.

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo apt install ffmpeg
```

## Launch

```bash
python3 server.py
```

Open `pitube.html` in your browser.
If the front is not running on the same machine than the Pi, edit the line `const API = 'http://localhost:5000'` in `pitube.html`.

## Updates

yt-dlp should be kept up-to-date, else the audio extraction might fail :

```bash
pip install -U yt-dlp
```
