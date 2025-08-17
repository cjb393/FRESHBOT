# FreshBot — Maps + Voice

FreshBot is a Discord bot that:
- Serves **D&D maps** and **art** via fuzzy search (`/map`, `/art`)
- **Auto-compresses** local images to fit your server’s upload cap (default **10 MB**)
- (Optional) Records & transcribes voice (Whisper / faster-whisper)

---

## Prerequisites
- Windows + **Python 3.12**
- **ImageMagick** CLI (`magick`)
- Discord bot invited with: *Send Messages*, *Attach Files*, *Use Application Commands* (and *Connect*, *Speak* for voice)

Install ImageMagick (one time):

    winget install --id ImageMagick.ImageMagick -e --silent

After installing, open a **new terminal** so `magick` is on PATH.

---

## Setup

    cd C:\FRESHBOT
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt

Create `.env` (no inline `#` comments on value lines):

    DISCORD_TOKEN=YOUR_BOT_TOKEN
    WHISPER_MODEL_SIZE=small

Place assets (not tracked by git):

    C:\FRESHBOT\art
    C:\FRESHBOT\dnd_maps

---

## Run

### F5 in VS Code (recommended)
This repo includes `.vscode/tasks.json` and `.vscode/launch.json`.
- F5 → runs `compress.ps1` (shrinks images > **10 MB** to ~**9.5 MB**) → launches `app.py`.
- First time: Ctrl+Shift+P → “Python: Select Interpreter” → choose `.venv\Scripts\python.exe`.
- Stop with **Shift+F5** or **Ctrl+C** in the terminal.

### Manual

    powershell -NoProfile -ExecutionPolicy Bypass -File .\compress.ps1   # optional, fast
    .\.venv\Scripts\python.exe .\app.py

Stop with **Ctrl+C**.

---

## Slash Commands
- `/map <query>` – search `dnd_maps/` and upload
- `/art <query>` – search `art/` and upload
- `/assets` – show counts
- `/refresh_cache` – rebuild asset index
- *(Optional)* `/record`, `/stop` for voice

Tip: If you add/remove files while running, use `/refresh_cache`.

---

## Auto-Compression
- Script: `compress.ps1`
- Policy: compress any image **> 10 MB** to **~9.5 MB** (WEBP if transparent, JPEG otherwise)
- In-place overwrite (extension may change)
- Cache: `compression_cache.json` stores `path + lastWriteTime + size` so re-runs touch only **new/changed** files

Adjust limits in `compress.ps1` (top of file):

    $uploadLimitMB  = 10.0   # compress anything larger than this
    $targetMB       = 9.5    # final target; leave headroom

Examples:
- Nitro/Boost 50 MB → set `50.0 / 48.0`
- Boost 100 MB → set `100.0 / 95.0`

If you change limits and want to reprocess old files, delete `compression_cache.json` once.

---

## Troubleshooting
- `magick` not found → open a new terminal after installing ImageMagick.
- Whisper model error → ensure `.env` has `WHISPER_MODEL_SIZE=small` exactly.
- F5 won’t run → open the **C:\FRESHBOT** folder in VS Code (not just a file).
- Bot still “online” after stop → kill stray processes:

    taskkill /F /IM python.exe

---

## Git Hygiene
Ignored by `.gitignore`: `art/`, `dnd_maps/`, `recaps/`, `.env`, `.venv/`, `asset_cache.json`, `compression_cache.json`, logs.

Typical workflow:

    git add -A
    git commit -m "Stable: asset search + auto-compress + F5 start"
    git push

---

## Folder Layout

    C:\FRESHBOT
    ├─ app.py
    ├─ asset_commands.py
    ├─ compress.ps1
    ├─ requirements.txt
    ├─ .env                      # not committed
    ├─ compression_cache.json    # generated
    ├─ asset_cache.json          # generated
    ├─ art/                      # your images (not committed)
    ├─ dnd_maps/                 # your maps   (not committed)
    └─ .vscode/
       ├─ tasks.json             # F5 prelaunch: runs compress.ps1
       └─ launch.json            # F5: starts app.py after compression
