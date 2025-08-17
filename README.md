# FreshBot — Maps + Voice

FreshBot is a Discord bot that:
- Serves **D&D maps** and **art** via fuzzy search (`/map`, `/art`)
- **Auto-compresses** local images to fit your server’s upload cap (default **10 MB**)
- Optional: records and transcribes voice using **faster-whisper**

---

## Prerequisites

- Windows
- **Python 3.12** (installed system-wide)
- **ImageMagick** CLI (`magick`) available on PATH
- A Discord application/bot with these permissions:
  - Send Messages
  - Attach Files
  - Use Application Commands
  - (Optional for voice) Connect, Speak

Install ImageMagick (one time):

```powershell
winget install --id ImageMagick.ImageMagick -e --silent
````

Open a **new** terminal after installing so `magick` is on PATH.

---

## Setup

```powershell
cd C:\FRESHBOT
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip setuptools wheel
pip install -r requirements.txt
```

Create `.env` (no inline `#` on value lines):

```
DISCORD_TOKEN=YOUR_BOT_TOKEN
WHISPER_MODEL_SIZE=small
```

Place assets (not tracked by git):

```
C:\FRESHBOT\art
C:\FRESHBOT\dnd_maps
```

---

## VS Code configuration (stops Pylance “missing import” noise)

1. Ctrl+Shift+P → **Python: Select Interpreter** → choose
   `C:\FRESHBOT\.venv\Scripts\python.exe`
2. Optional workspace pin: create `.vscode/settings.json` with:

```json
{
  "python.defaultInterpreterPath": "C:\\FRESHBOT\\.venv\\Scripts\\python.exe",
  "python.terminal.activateEnvironment": true,
  "python.analysis.extraPaths": ["C:\\FRESHBOT\\.venv\\Lib\\site-packages"]
}
```

3. Ctrl+Shift+P → **Developer: Reload Window**

---

## Run

### F5 in VS Code (recommended)

This repo includes `.vscode/tasks.json` and `.vscode/launch.json`.

* F5 runs `compress.ps1` first, then launches `app.py`
* Stop with Shift+F5 or Ctrl+C in the terminal

### Manual

```powershell
# Optional pre-compression (fast, skips unchanged)
powershell -NoProfile -ExecutionPolicy Bypass -File .\compress.ps1

# Start the bot
.\.venv\Scripts\python.exe .\app.py
```

---

## Slash Commands

* `/map <query>` — search and upload from `dnd_maps/`
* `/art <query>` — search and upload from `art/`
* `/assets` — show counts
* `/refresh_cache` — rebuild asset index
* Optional voice: `/record`, `/stop`

Tip: If you add/remove files while running, use `/refresh_cache`.

---

## Auto-Compression

* Script: `compress.ps1`
* Policy: compress any image **> 10 MB** to **\~9.5 MB**

  * Uses WEBP if transparent, JPEG otherwise
  * In-place overwrite (extension may change)
* Cache: `compression_cache.json` tracks `path + lastWriteTime + size`

Adjust limits at the top of `compress.ps1`:

```powershell
$uploadLimitMB  = 10.0   # compress anything larger than this
$targetMB       = 9.5    # final size target
```

Examples:

* Nitro/Boost 50 MB → set `50.0 / 48.0`
* Boost 100 MB → set `100.0 / 95.0`

If you change limits and want to reprocess old files, delete `compression_cache.json` once.

---

## Verify

```powershell
magick -version | Select-Object -First 3

# quick Python verify
.\.venv\Scripts\python.exe - << 'PY'
import discord, numpy
from faster_whisper import WhisperModel
print("discord", getattr(discord, "__version__", "git"))
print("numpy", numpy.__version__)
print("faster-whisper OK:", WhisperModel is not None)
PY
```

---

## Troubleshooting

* `magick` not found → open a **new** terminal after install
* Pylance “missing imports” → select interpreter `C:\FRESHBOT\.venv\Scripts\python.exe` and reload window
* Voice errors about Opus → `PyNaCl` is installed; if you still see Opus errors at runtime, install an Opus DLL and ensure it’s on PATH
* Bot still “online” after stop → kill stray processes:

```powershell
taskkill /F /IM python.exe
```

---

## Git Hygiene

Ignored by `.gitignore`: `art/`, `dnd_maps/`, `recaps/`, `.env`, `.venv/`, `asset_cache.json`, `compression_cache.json`, logs.

Typical workflow:

```powershell
git add -A
git commit -m "Stable: asset search + auto-compress + F5 start"
git push
```

---

## Folder Layout

```
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
```

```
```