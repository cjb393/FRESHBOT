# app.py
from __future__ import annotations

import os
import sys
import asyncio
import logging
import contextlib
import threading
import time
from typing import Dict, Optional, Tuple, Deque, Any
from collections import deque

import numpy as np

import discord
from discord import app_commands
from discord.abc import Messageable
from asset_commands import setup_asset_commands, add_stats_command, AssetCommands

# --- NEW: load .env before reading env vars ---
from dotenv import load_dotenv
load_dotenv()

# voice receiving extension
from discord.ext import voice_recv  # pip install -U discord-ext-voice-recv
# speech-to-text
from faster_whisper import WhisperModel

# ------------ logging ------------
logger = logging.getLogger("freshbot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)

# ------------ config ------------
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN:
    print("ERROR: DISCORD_TOKEN is missing in environment (.env).", file=sys.stderr)
    sys.exit(1)

LANG = os.getenv("LANGUAGE", "en").strip() or "en"
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL", "small").strip() or "small"

# audio pipeline
SR_IN = 48000            # Discord PCM (decoded)
SR_OUT = 16000           # whisper preferred
CHUNK_SECONDS = 5        # transcribe every ~N seconds per speaker
MIN_SECONDS_TO_POST = 2  # don't post < 2s audio

# ------------ discord client ------------
intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ------------ whisper ------------
logger.info("Loading Whisper model: %s", WHISPER_MODEL_SIZE)
_model = WhisperModel(
    WHISPER_MODEL_SIZE,
    device="auto",
    compute_type="auto",  # will fall back to float32 if needed
)
logger.info("Loaded Whisper model: %s", WHISPER_MODEL_SIZE)

# ------------ helpers ------------

def _choose_post_channel(interaction: discord.Interaction) -> Optional[Messageable]:
    """Pick a messageable text destination with permission to send."""
    ch = interaction.channel
    if isinstance(ch, (discord.TextChannel, discord.Thread, discord.DMChannel)):
        return ch

    g = interaction.guild
    if g is None:
        return None

    # prefer system channel if sendable
    sys_ch = g.system_channel
    if isinstance(sys_ch, discord.TextChannel):
        me = g.me
        if me and sys_ch.permissions_for(me).send_messages:
            return sys_ch

    # otherwise first sendable text channel
    me = g.me
    for tc in g.text_channels:
        with contextlib.suppress(Exception):
            if me and tc.permissions_for(me).send_messages:
                return tc

    return None


def _display_name(entity: Any) -> str:
    """Human-friendly display name for members/users/fallbacks."""
    if hasattr(entity, "display_name") and isinstance(getattr(entity, "display_name"), str):
        return getattr(entity, "display_name")
    if hasattr(entity, "name") and isinstance(getattr(entity, "name"), str):
        return getattr(entity, "name")
    return "unknown"


def _downmix_stereo_to_mono_int16(stereo: np.ndarray) -> np.ndarray:
    """(N, 2) int16 -> (N,) int16"""
    if stereo.ndim == 1:
        return stereo
    if stereo.shape[-1] == 2:
        # average with int32 to avoid overflow
        return ((stereo.astype(np.int32).sum(axis=1) // 2)).astype(np.int16)
    # unknown channel count: just take first
    return stereo[..., 0].astype(np.int16)


def _resample_48k_to_16k_mono_int16(mono_48k: np.ndarray) -> np.ndarray:
    """Very simple decimation by 3. Assumes mono int16 @ 48k."""
    # Take every 3rd sample (naive but works fine for speech)
    return mono_48k[::3].copy()


def _pcm_bytes_to_int16_array(pcm: bytes) -> np.ndarray:
    """bytes -> int16 ndarray"""
    return np.frombuffer(pcm, dtype=np.int16)


# ------------ Transcribe sink (runs on decoder thread) ------------

class TranscribeSink(voice_recv.AudioSink):  # type: ignore
    """Collects PCM per speaker; an asyncio worker handles transcribe + posting."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        post_channel: Messageable,
        model: WhisperModel,
        sr_in: int = SR_IN,
        sr_out: int = SR_OUT,
    ) -> None:
        super().__init__()
        self.loop = loop
        self.post_channel = post_channel
        self.model = model
        self.sr_in = sr_in
        self.sr_out = sr_out

        # thread-safe buffers keyed by user_id (or ssrc fallback)
        self._lock = threading.Lock()
        self._buffers: Dict[str, Deque[np.ndarray]] = {}
        self._names: Dict[str, str] = {}

        # control
        self._running = threading.Event()
        self._running.set()
        self._worker_task: Optional[asyncio.Task[None]] = None

    # ---- AudioSink API (required by discord-ext-voice-recv) ----

    def wants_opus(self) -> bool:
        # Return False to receive decoded PCM (int16 48k)
        return False

    def write(self, source: Any, data: Any) -> None:
        """Called from router thread for every RTP payload (per speaker)."""
        try:
            pcm: Optional[bytes] = getattr(data, "pcm", None)
            if not pcm:
                return

            arr = _pcm_bytes_to_int16_array(pcm)

            # If stereo int16, downmix to mono
            if arr.ndim == 1:
                # Try to infer channels: Discord typically delivers 2ch interleaved
                if len(arr) % 2 == 0:
                    arr = arr.reshape(-1, 2)
                    arr = _downmix_stereo_to_mono_int16(arr)
            else:
                arr = _downmix_stereo_to_mono_int16(arr)

            # Downsample 48k -> 16k (naive decimation)
            arr16 = _resample_48k_to_16k_mono_int16(arr)

            # identify speaker key + display name (best-effort)
            key = None
            name = "unknown"
            if source is not None:
                key = str(getattr(source, "id", None) or getattr(source, "ssrc", None) or "unknown")
                name = _display_name(source)
            if not key:
                key = "unknown"

            with self._lock:
                if key not in self._buffers:
                    self._buffers[key] = deque()
                    self._names[key] = name
                self._buffers[key].append(arr16)
        except Exception as e:
            logger.exception("TranscribeSink.write error: %s", e)

    def cleanup(self) -> None:
        """Called by library when listening stops."""
        try:
            self._running.clear()
        except Exception:
            pass

    # ---- lifecycle from our side ----

    def start_worker(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.run_coroutine_threadsafe(self._worker(), self.loop)  # type: ignore[arg-type]

    async def stop_worker(self) -> None:
        self._running.clear()
        if self._worker_task:
            self._worker_task = None

    async def _worker(self) -> None:
        """Async task on event loop: periodically drains speaker buffers, transcribes, posts."""
        CHUNK = CHUNK_SECONDS * SR_OUT
        MIN_POST = max(int(MIN_SECONDS_TO_POST * SR_OUT), 1)

        while self._running.is_set():
            await asyncio.sleep(1.0)
            # snapshot buffers thread-safely
            with self._lock:
                keys = list(self._buffers.keys())

            for key in keys:
                with self._lock:
                    dq = self._buffers.get(key)
                    name = self._names.get(key, "unknown")
                    if not dq:
                        continue
                    # collect enough samples for one chunk
                    total = sum(len(x) for x in dq)
                    if total < CHUNK:
                        continue
                    # pop to produce ~CHUNK samples
                    samples: list[np.ndarray] = []
                    have = 0
                    while dq and have < CHUNK:
                        part = dq.popleft()
                        samples.append(part)
                        have += len(part)

                if not samples:
                    continue

                mono16 = np.concatenate(samples)
                if len(mono16) < MIN_POST:
                    continue

                # convert to float32 in [-1, 1]
                audio_f32 = (mono16.astype(np.float32) / 32768.0)

                # transcribe in thread pool
                try:
                    loop = asyncio.get_running_loop()
                    text = await loop.run_in_executor(None, _do_transcribe, self.model, audio_f32, LANG)
                except Exception as e:
                    logger.exception("Transcribe error for %s: %s", name, e)
                    continue

                text = (text or "").strip()
                if not text:
                    continue

                # post result
                try:
                    await self.post_channel.send(f"**{name}:** {text}")
                except Exception as e:
                    logger.exception("Failed to post transcript: %s", e)


def _do_transcribe(model: WhisperModel, audio_f32: np.ndarray, lang: str) -> str:
    """Blocking transcription; returns plain text from segments."""
    segments, info = model.transcribe(
        audio_f32,
        language=lang,
        vad_filter=True,
        beam_size=1,
        vad_parameters=dict(min_silence_duration_ms=250),
        condition_on_previous_text=False,
    )
    out = []
    for seg in segments:
        t = (seg.text or "").strip()
        if t:
            out.append(t)
    return " ".join(out).strip()


# ------------ session management per guild ------------
class Session:
    def __init__(self, vc: voice_recv.VoiceRecvClient, sink: TranscribeSink) -> None:  # type: ignore
        self.vc = vc
        self.sink = sink

SESSIONS: Dict[int, Session] = {}


# ------------ commands ------------

@client.event
async def setup_hook() -> None:
    start_time = time.time()

    # Original transcription commands
    @tree.command(name="record", description="Start/continue recording this voice channel and post transcripts.")
    async def record_cmd(interaction: discord.Interaction) -> None:
        await _cmd_record(interaction)

    @tree.command(name="stop", description="Stop recording in this server.")
    async def stop_cmd(interaction: discord.Interaction) -> None:
        await _cmd_stop(interaction)

    # NEW: Set up asset commands (don't wait for cache initialization)
    logger.info("Setting up asset commands...")
    asset_commands: AssetCommands = setup_asset_commands(client, tree)
    add_stats_command(tree, asset_commands)  # Optional stats command
    
    # Debug: Print all registered commands
    for cmd in tree.get_commands():
        logger.info(f"Registered command: {cmd.name}")
    
    # Single sync call
    await tree.sync()
    logger.info("Synced global commands (including asset search).")

    total_time = time.time() - start_time
    logger.info(f"Setup completed in {total_time:.2f} seconds")

@client.event
async def on_ready() -> None:
    print(f"READY as {client.user}")

async def _cmd_record(interaction: discord.Interaction) -> None:
    g = interaction.guild
    if g is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    state = getattr(interaction.user, "voice", None)
    channel = getattr(state, "channel", None)
    if channel is None or not isinstance(channel, discord.VoiceChannel):
        await interaction.response.send_message("You’re not in a voice channel.", ephemeral=True)
        return

    post_channel = _choose_post_channel(interaction)
    if post_channel is None:
        await interaction.response.send_message(
            "I can't find a text channel I can post to.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=False)

    vc_any = g.voice_client
    if isinstance(vc_any, voice_recv.VoiceRecvClient):  # type: ignore
        vc = vc_any
        if vc.channel.id != channel.id:
            await vc.move_to(channel)
    else:
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=True)  # type: ignore

    existing = SESSIONS.get(g.id)
    if existing:
        try:
            existing.sink.start_worker()
        except Exception:
            pass
        await interaction.followup.send(
            f"Recording in **{channel.name}** and posting to {getattr(post_channel, 'mention', '#text-channel')}.",
            ephemeral=True,
        )
        return

    sink = TranscribeSink(loop=asyncio.get_running_loop(), post_channel=post_channel, model=_model)
    sink.start_worker()
    vc.listen(sink)  # type: ignore[attr-defined]
    SESSIONS[g.id] = Session(vc=vc, sink=sink)

    await interaction.followup.send(
        f"Recording in **{channel.name}** and posting to {getattr(post_channel, 'mention', '#text-channel')}.",
        ephemeral=True,
    )

async def _cmd_stop(interaction: discord.Interaction) -> None:
    g = interaction.guild
    if g is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=False)

    sess = SESSIONS.pop(g.id, None)
    if sess is not None:
        with contextlib.suppress(Exception):
            await sess.sink.stop_worker()
        with contextlib.suppress(Exception):
            await sess.vc.disconnect(force=False)

        await interaction.followup.send("Stopped recording and left the voice channel.", ephemeral=True)
        return

    vc = g.voice_client
    if vc is not None:
        with contextlib.suppress(Exception):
            await vc.disconnect(force=False)

    await interaction.followup.send("Nothing to stop here.", ephemeral=True)


# ------------ entry ------------
if __name__ == "__main__":
    client.run(TOKEN)
