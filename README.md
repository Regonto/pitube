# PiTube

Lightweight YouTube player for Raspberry Pi.
Static HTML front-end + Flask/yt-dlp backend. Audio by default; an optional muted video can be shown with `--video`.

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo apt install ffmpeg mpv
```

## Usage

### Normal mode
Each client plays the audio locally in its own browser.
```bash
python3 server.py
```

### Party mode
The audio plays on the Pi through mpv. All connected clients share the same queue and state in real time.
```bash
python3 server.py --party
```

Open `http://<pi-ip>:5000` from any device on the network.

#### Starting and stopping the party live
The party does not need a restart: click the **PiTube logo** in the header.
- **Normal mode**: the logo is greyed out. Clicking it opens a window offering an optional 4-digit PIN and a **Launch!** button (or the cross to cancel).
- **Party mode**: the logo is in colour and the header shows a **PARTY 🎉** label. Clicking the logo opens a window to **Stop** the party (it asks for the PIN if one was set; without a PIN, anyone can stop it, just like anyone can start it, even while the party is locked).
- Starting the party starts with an empty queue. Stopping it stops the music, clears the queue and unlocks the party.
- Every other device notices the change within a few seconds and reloads itself in the right mode (so audio playing in a browser in normal mode is interrupted).
- The PIN chosen when starting is the one used to lock / unlock / stop the party (see *Party lock*). Starting with `python3 server.py --party -pin 1234` does the same thing.
- A long-click (or right-click) on the logo still shows / hides the QR code instead of opening the window.

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
- The button opens the video under the search bar and above the player, as large as possible while keeping its aspect ratio.
- Launching a search shrinks it to a small player at the bottom right (click it, or the size button, to enlarge it again).
- Fullscreen: button on the video, double-click, or the **F** key (**F** again or Esc to leave).
- The video is a muted copy kept in sync with the music (the sound still comes from mpv / the browser).
- It comes from the same YouTube lookup as the audio (no extra request), and nothing is downloaded while it is hidden.
- If a video fails, it is retried twice with a fresh link, then the reason is shown on screen and printed in the server console (`[video] ...`).
  Most failures come from YouTube itself: keep yt-dlp up to date with `pip install -U yt-dlp`.
- Quality is capped at 720p (H.264 first, AV1 avoided: lighter for a Pi). Change it with `PITUBE_VIDEO_HEIGHT=1080 python3 server.py --video`.

#### The Pi's own screen (party mode + `--video`)
- When a track is started by hand from any device (play, "Play now", a click in the queue or in a playlist), the video also opens on the Pi's own screen (big; if the small player is already up it just changes its video).
- Opening the page on any device while a track is playing also shows its video. Nothing opens when the next track starts by itself.
- The Pi is recognised when it connects through `localhost` or its own IP; otherwise open the page with `?tv` (e.g. `http://192.168.1.42:5000/?tv`).

### Keyboard and touch controls
| Action | Keyboard | Touch / mouse |
|---|---|---|
| +10 s / -10 s | Right / Left arrow | double-tap the right / left half of the video |
| Volume +5 % / -5 % | Up / Down arrow (while the big video is up) | |
| x2 speed | | hold 1 s on the video (mouse or finger), until you let go |
| Fullscreen | F | button, or double-click |
| Close panel / video / window | Esc | |

- Esc closes the start/stop window, then the right-hand panel if it is open, otherwise hides the video (in fullscreen it just leaves fullscreen).
- "Play now" puts the track right after the current one and starts it.
- Left / Right work anywhere (except while typing in a field); Up / Down only while the big video is up, otherwise they keep scrolling the page.
- Launching a track opens the video (big) if it is hidden. If the small player is up it stays small and only its video changes. Automatic track changes never open anything.
- In party mode the x2 speed is applied to mpv, and falls back to x1 by itself if the device that asked for it disappears.

### Theme
A sun / moon button at the top right of the header switches between the dark and light themes (hidden while the side panel is open).
The choice is remembered in the browser; on first load the theme follows the system setting. Colours are CSS variables (`:root` and `:root[data-theme="light"]`).

### Saved playlists are shared
Saved playlists live on the server (`playlists.json`). Every change (add / remove a track, create, delete) is applied there one at a time,
so several devices can edit at the same time without overwriting each other.
In party mode, every connected device refreshes within a second when someone changes a playlist.

### Party lock
In party mode, the padlock under the QR code locks the party. Whoever locks it becomes the *master*
(identified by a random id kept in their browser) and is the only one who can unlock it.
While locked, everybody else can only:
- add tracks to the **end of the queue**
- add tracks to a **saved playlist** (creating playlists and adding tracks is fine, removing is refused)

Everything else (play, skip, pause, seek, volume, reorder, remove, clear...) is refused by the server.
The Pi itself (a browser on `localhost`) is always master, so a lock can never get stuck;
stopping the party or restarting the server also resets it.

**Optional PIN**: with a 4-digit code, locking, unlocking and stopping the party ask for it.
Set it when starting the party from the page, or on the command line:
```bash
python3 server.py --party -pin 1234
```
With a PIN, anyone who knows the code can lock, unlock or stop (from any device, the Pi included), so a guest can't lock the party before you do.
Without a PIN, no code is asked. On the command line, `-pin` is ignored (with a warning) without `--party`.
After 5 wrong codes from the same address, that address is locked out for 60 seconds.

Logos are read from `assets/logo.png` (normal mode, shown greyed out) and `assets/logo_party.png` (party mode).

## Security
- There is no SQL database (playlists are stored in `playlists.json`), so SQL injection is not possible. The search text is only passed to yt-dlp as `ytsearchN:<text>`, never to a shell.
- No shell command is built from user input: mpv is started with an argument list, and the stream URL comes from yt-dlp, never from a client.
- Every track received from a client (queue, playlists) is filtered by the server: valid YouTube id, truncated texts, thumbnail forced to `ytimg.com`.
- In the page, every text coming from the network (titles, channels, playlist names) is escaped before it is displayed (XSS protection).
- No CORS (another website cannot control the party), plus `Content-Security-Policy`, `X-Frame-Options` and `nosniff` headers.
- Starting / stopping the party and locking are protected by the optional PIN, with brute-force lockout.
- Flask's development server is meant for a trusted local network: do not expose it as is on the Internet (put an HTTPS reverse proxy with authentication in front of it).

## Notes
- Keep yt-dlp up to date or audio extraction will break: `pip install -U yt-dlp`
- Party mode requires `mpv` installed on the Pi
- Saved playlists are stored in `playlists.json` next to `server.py`
