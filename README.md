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

### Video
Start the server with `--video` (alone or with `--party`) to get a video button in the player:
```bash
python3 server.py --party --video
```
- The button opens the video under the search bar and above the player, as large as possible with its own aspect ratio.
- Launching a search shrinks it to a small player at the bottom right (click it, or the size button, to enlarge it again).
- Fullscreen: button on the video, double-click, or the **F** key (**F** again or Esc to leave).
- The video is a muted copy kept in sync with the music (the sound still comes from mpv / the browser).
- It comes from the same YouTube lookup as the audio (no extra request), and nothing is downloaded while it is hidden.
- If a video fails, it is retried twice with a fresh link, then the reason is shown on screen and printed in the server console (`[video] ...`).
  Most failures are on YouTube's side: keep yt-dlp up to date with `pip install -U yt-dlp`.
- Quality is capped at 720p (H.264 first, AV1 avoided: light for a Pi). Change it with `PITUBE_VIDEO_HEIGHT=1080 python3 server.py --video`.

### Keyboard and touch controls
| Action | Keyboard | Touch / mouse |
|---|---|---|
| +10 s / -10 s | Right / Left arrow | double-tap the right / left half of the video |
| Volume +5 % / -5 % | Up / Down arrow (while the big video is up) | |
| x2 speed | | hold 1 s on the video (mouse or finger), until you let go |
| Fullscreen | F | button, or double-click |
| Close panel / video | Esc | |

- Esc closes the right-hand panel if it is open, otherwise hides the video (in fullscreen it just leaves fullscreen).
- "Play now" puts the track right after the current one and starts it.
- Left / Right work anywhere (except while typing in a field); Up / Down only while the big video is up, otherwise they keep scrolling the page.
- Launching a track opens the video (big) if it is hidden. If the small player is up it stays small and only its video changes. Automatic changes of track never open anything.
- In party mode the x2 speed is applied to mpv, and falls back to x1 by itself if the device that asked for it disappears.

### Saved playlists are shared
Saved playlists live on the server (`playlists.json`) and every change (add / remove a track, create, delete)
is applied there one at a time, so several devices can edit at once without overwriting each other.
In party mode, every connected device refreshes within a second when someone changes a playlist.

### Party lock
In party mode, the padlock under the QR code locks the party. Whoever locks it becomes the *master*
(identified by a random id kept in their browser) and is the only one who can unlock it.
While locked, everybody else can only:
- add tracks to the **end of the queue**
- add tracks to a **saved playlist** (creating playlists / adding tracks is fine, removing is refused)

Everything else (play, skip, pause, seek, volume, reorder, remove, clear...) is refused by the server.
The Pi itself (a browser on `localhost`) is always master, so a lock can never get stuck;
restarting the server also resets it.

**Optional PIN** (party mode only): start the server with a 4-digit code and locking / unlocking will ask for it.
```bash
python3 server.py --party -pin 1234
```
With a PIN, anyone who knows the code can lock or unlock (from any device, the Pi included), so a guest can't
lock the party before you do. Without `-pin`, no code is asked. `-pin` is ignored (with a warning) without `--party`.
After 5 wrong codes from the same address, that address is locked out for 60 seconds.

Logos are read from `assets/logo.png` (normal) and `assets/logo_party.png` (party).

## Notes

- Keep yt-dlp up to date or audio extraction will break: `pip install -U yt-dlp`
- Party mode requires `mpv` installed on the Pi
- Saved playlists are stored in `playlists.json` next to `server.py`
