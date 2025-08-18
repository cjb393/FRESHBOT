````markdown
# FreshBot — Maps + Voice + Transcripts

FreshBot is a Discord bot that:
- Serves **D&D maps** and **art** via fuzzy search (`/map`, `/art`)
- **Auto-compresses** local images to fit your server’s upload cap (default **10 MB**)
- Records & transcribes voice with **faster-whisper**, and writes a clean **.txt transcript** per session (uploaded on `/stop`)

---

## Prerequisites

- **Windows**
- **Python 3.12** (installed system-wide)
- **ImageMagick** CLI (`magick`) on PATH
- A Discord application/bot with permissions:
  - Send Messages, Attach Files, Use Application Commands
  - (Optional for voice) Connect, Speak

Install ImageMagick (one time):

```powershell
winget install --id ImageMagick.ImageMagick -e --silent
````

> Open a **new terminal** after installing so `magick` is on PATH.

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
WHISPER_MODEL=small
LANGUAGE=en
```

Place assets (not tracked by git):

```
C:\FRESHBOT\art
C:\FRESHBOT\dnd_maps
```

---

## VS Code (silence Pylance import noise)

1. **Ctrl+Shift+P → Python: Select Interpreter** → `C:\FRESHBOT\.venv\Scripts\python.exe`
2. Optional `.vscode/settings.json`:

```json
{
  "python.defaultInterpreterPath": "C:\\FRESHBOT\\.venv\\Scripts\\python.exe",
  "python.terminal.activateEnvironment": true
}
```

---

## Run

### F5 in VS Code (recommended)

Repo includes `.vscode/tasks.json` + `.vscode/launch.json`.

* **F5** runs `compress.ps1` first, then launches `app.py`
* Stop with **Shift+F5** or **Ctrl+C** in the terminal

### Manual

```powershell
# Optional pre-compression (fast; skips unchanged)
powershell -NoProfile -ExecutionPolicy Bypass -File .\compress.ps1

# Start the bot
.\.venv\Scripts\python.exe .\app.py
```

---

## Slash Commands

* `/map <query>` — search & upload from `dnd_maps/`
* `/art <query>` — search & upload from `art/` (PDFs supported as attachments)
* `/assets` — show cached counts
* `/refresh_cache` — rebuild asset index
* `/record` — start/continue recording the voice channel and post transcripts
* `/stop` — stop, leave voice, **upload session transcript .txt**
* `/transcript` — upload the current or most recent transcript file

> If you add/remove files while running, use **/refresh\_cache**.

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

Examples: 50 MB cap → `50.0 / 48.0`; 100 MB cap → `100.0 / 95.0`.
If you change limits and want to reprocess old files, delete `compression_cache.json` once.

**Note on PDFs:** PDFs are discoverable and uploadable via `/art`, but the compressor does **not** shrink PDFs.

---

## Transcripts

* One UTF-8 `.txt` per session is written to **`transcripts/`**:

  * Filename includes date, guild, channel, and UTC start time.
  * Every line posted to Discord is appended with `[HH:MM:SS] Speaker: text`.
* On `/stop`, the `.txt` is **uploaded** to the text channel and left on disk.
* `/transcript` can upload the current/most recent log on demand.
* Audio is **not** saved—only the text.

**Git ignore:** keep transcripts out of the repo. In `.gitignore`:

```
transcripts/
```

If you accidentally tracked any:

```powershell
git rm -r --cached transcripts
git commit -m "chore: ignore transcripts directory"
git push
```

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

* `magick` not found → open a **new terminal** after installing ImageMagick.
* Pylance “missing imports” → select the `.venv` interpreter and reload window.
* Opus/voice issues → `discord.py[voice]` installs `PyNaCl`; ensure any OS Opus DLL is on PATH if required.
* Bot still “online” after stop → kill stray processes:

```powershell
taskkill /F /IM python.exe
```

---

## Git Hygiene

Ignored by `.gitignore`: `art/`, `dnd_maps/`, `recaps/`, `transcripts/`, `.env`, `.venv/`, `asset_cache.json`, `compression_cache.json`, logs.

Typical workflow:

```powershell
git add -A
git commit -m "Stable: maps/art, auto-compress, transcripts"
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
├─ transcripts/              # generated, ignored by git
├─ art/                      # your images (not committed)
├─ dnd_maps/                 # your maps   (not committed)
└─ .vscode/
   ├─ tasks.json             # F5 prelaunch: runs compress.ps1
   └─ launch.json            # F5: starts app.py after compression
```

```
::contentReference[oaicite:0]{index=0}
```
