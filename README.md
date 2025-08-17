# FreshBot — Voice Probe

## Prereqs
- Windows + Python 3.12
- A Discord bot invited to your server (with **Connect**, **View Channels**, **Speak**).

## Setup
Inside this folder:
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -U "discord.py[voice] @ git+https://github.com/Rapptz/discord.py@master" "aiohttp<3.10" python-dotenv

## .env
    DISCORD_TOKEN=YOUR_BOT_TOKEN
    GUILD_ID=123456789012345678
    VOICE_CHANNEL_ID=123456789012345678
    TEXT_CHANNEL_ID=123456789012345678

## Run
    python .\probe_voice.py

If it prints `connected: True`, the voice link works.
