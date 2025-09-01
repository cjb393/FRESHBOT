from __future__ import annotations

import os
import sys
import asyncio
import logging
import contextlib
import threading
import time
from typing import Dict, Optional, Tuple, Deque, Any, List
from collections import deque
from pathlib import Path
from datetime import datetime, timezone
import concurrent.futures

from asset_commands import setup_asset_commands, add_stats_command

import numpy as np

import discord
from discord import app_commands
from discord.abc import Messageable
from discord.ext import voice_recv  # pip install -U discord-ext-voice-recv
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

SR_IN = 48000
SR_OUT = 16000
CHUNK_SECONDS = 5
IDLE_FLUSH_SECONDS = 2.0
MAX_BUFFER_SECONDS = 6.0
MIN_SECONDS_TO_POST = 1.6
TRANSCRIPTS_DIR = Path(os.getenv("TRANSCRIPTS_DIR", "transcripts"))
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = False

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

logger.info(f"Loading Whisper model: {WHISPER_MODEL_SIZE}")
_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")

class TranscriptLogger:
    def __init__(self, root: Path = TRANSCRIPTS_DIR) -> None:
        self.root = root
        self._open_files: Dict[int, Path] = {}

    @staticmethod
    def _safe(name: str) -> str:
        return "".join(c for c in name if c.isalnum() or c in (" ", "_", "-", ".")).strip().replace(" ", "_")

    def start_session(self, guild: Optional[discord.Guild], channel: Optional[discord.abc.GuildChannel]) -> Path:
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        gname = self._safe(guild.name) if guild and guild.name else f"guild_{getattr(guild, 'id', 'unknown')}"
        cname = self._safe(channel.name) if channel and hasattr(channel, "name") else f"chan_{getattr(channel,'id','unknown')}"
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        path = self.root / f"{date}_{gname}_{cname}_{stamp}.txt"
        header = (
            f"# FreshBot Transcript\n"
            f"# Guild: {guild.name if guild else 'Unknown'} (id={getattr(guild,'id','?')})\n"
            f"# Channel: {getattr(channel,'name','Unknown')} (id={getattr(channel,'id','?')})\n"
            f"# UTC Start: {datetime.now(timezone.utc).isoformat()}\n"
            f"# ------------------------------------------------------------\n"
        )
        path.write_text(header, encoding="utf-8")
        if guild and guild.id is not None:
            self._open_files[guild.id] = path
        return path

    def append_line(self, guild_id: int, text: str, speaker: Optional[str] = None, ts: Optional[float] = None, fallback_path: Optional[Path] = None) -> None:
        path = self._open_files.get(guild_id)
        if not path and fallback_path and fallback_path.exists():
            self._open_files[guild_id] = fallback_path
            path = fallback_path
        if not path:
            return
        if ts is not None:
            tstr = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
        else:
            tstr = datetime.now(timezone.utc).strftime("%H:%M:%S")
        who = f"{speaker}: " if speaker else ""
        line = f"[{tstr}] {who}{text}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    def stop_session(self, guild_id: int) -> Optional[Path]:
        path = self._open_files.pop(guild_id, None)
        if not path:
            return None
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"# UTC End: {datetime.now(timezone.utc).isoformat()}\n")
        return path

transcripts = TranscriptLogger()

def _choose_post_channel(interaction: discord.Interaction) -> Optional[Messageable]:
    ch = interaction.channel
    if isinstance(ch, (discord.TextChannel, discord.Thread, discord.DMChannel)):
        return ch
    g = interaction.guild
    if g is None:
        return None
    sys_ch = g.system_channel
    if isinstance(sys_ch, discord.TextChannel):
        me = g.me
        if me and sys_ch.permissions_for(me).send_messages:
            return sys_ch
    me = g.me
    for tc in g.text_channels:
        if me and tc.permissions_for(me).send_messages:
            return tc
    return None

def _display_name(entity: Any) -> str:
    if hasattr(entity, "display_name") and isinstance(getattr(entity, "display_name"), str):
        return getattr(entity, "display_name")
    if hasattr(entity, "name") and isinstance(getattr(entity, "name"), str):
        return getattr(entity, "name")
    return f"user_{getattr(entity, 'id', 'unknown')}"

def _downmix_stereo_to_mono_int16(stereo: np.ndarray) -> np.ndarray:
    if stereo.ndim == 1:
        return stereo
    if stereo.shape[-1] == 2:
        return ((stereo.astype(np.int32).sum(axis=1) // 2)).astype(np.int16)
    return stereo[..., 0].astype(np.int16)

def _resample_48k_to_16k_mono_int16(mono_48k: np.ndarray) -> np.ndarray:
    return mono_48k[::3].copy()

def _pcm_bytes_to_int16_array(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16)

def _do_transcribe(model: WhisperModel, audio_f32: np.ndarray, lang: str) -> str:
    segments, _ = model.transcribe(
        audio_f32,
        language=lang,
        vad_filter=True,
        beam_size=1,
        vad_parameters=dict(min_silence_duration_ms=250),
        condition_on_previous_text=False,
    )
    out: List[str] = []
    for seg in segments:
        t = (seg.text or "").strip()
        if t:
            out.append(t)
    return " ".join(out).strip()

def _get_with_timeout(fut: concurrent.futures.Future, timeout: float):
    return fut.result(timeout=timeout)

class TranscribeSink(voice_recv.AudioSink):  # type: ignore
    def __init__(self, loop: asyncio.AbstractEventLoop, post_channel: Messageable, model: WhisperModel, guild_id: int, transcript_path: Path, sr_in: int = SR_IN, sr_out: int = SR_OUT) -> None:
        super().__init__()
        self.loop = loop
        self.post_channel = post_channel
        self.model = model
        self.guild_id = guild_id
        self.transcript_path = transcript_path
        self.sr_in = sr_in
        self.sr_out = sr_out
        self._lock = threading.Lock()
        self._buffers: Dict[str, Deque[np.ndarray]] = {}
        self._names: Dict[str, str] = {}
        self._last_activity: Dict[str, float] = {}
        self._running = threading.Event()
        self._running.set()
        self._worker_future: Optional[concurrent.futures.Future] = None
        self._stopped_accepting = threading.Event()
        self._stopped_accepting.clear()

    def wants_opus(self) -> bool:
        return False

    def write(self, source: Any, data: Any) -> None:
        try:
            if self._stopped_accepting.is_set():
                return
            pcm: Optional[bytes] = getattr(data, "pcm", None)
            if not pcm:
                return
            arr = _pcm_bytes_to_int16_array(pcm)
            if arr.ndim == 1 and len(arr) % 2 == 0:
                arr = arr.reshape(-1, 2)
                arr = _downmix_stereo_to_mono_int16(arr)
            else:
                arr = _downmix_stereo_to_mono_int16(arr)
            arr16 = _resample_48k_to_16k_mono_int16(arr)
            key = str(getattr(source, "id", None) or getattr(source, "ssrc", None) or "unknown")
            name = _display_name(source) if source else "unknown"
            now = time.time()
            with self._lock:
                if key not in self._buffers:
                    self._buffers[key] = deque()
                    self._names[key] = name
                self._buffers[key].append(arr16)
                self._last_activity[key] = now
        except Exception as e:
            logger.exception("TranscribeSink.write error: %s", e)

    def cleanup(self) -> None:
        self._running.clear()
        self._stopped_accepting.set()

    def start_worker(self) -> None:
        if self._worker_future is None:
            self._worker_future = asyncio.run_coroutine_threadsafe(self._worker(), self.loop)  # type: ignore

    async def stop_accepting(self) -> None:
        self._stopped_accepting.set()

    async def stop_worker(self) -> None:
        self._running.clear()

    async def join_worker(self, timeout: float = 10.0) -> None:
        fut = self._worker_future
        self._worker_future = None
        if fut is None:
            return
        try:
            await asyncio.to_thread(_get_with_timeout, fut, timeout)
        except Exception as e:
            logger.warning("Worker join timed out or failed: %s", e)

    def _pop_all(self, dq: Deque[np.ndarray]) -> List[np.ndarray]:
        xs: List[np.ndarray] = []
        while dq:
            xs.append(dq.popleft())
        return xs

    async def _flush_speaker(self, key: str, reason: str) -> None:
        with self._lock:
            dq = self._buffers.get(key)
            name = self._names.get(key, "unknown")
            if not dq or len(dq) == 0:
                return
            samples = self._pop_all(dq)
        mono16 = np.concatenate(samples)
        if mono16.size == 0:
            return
        audio_f32 = (mono16.astype(np.float32) / 32768.0)
        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, _do_transcribe, self.model, audio_f32, LANG)
        except Exception as e:
            logger.exception("Transcribe error (flush=%s) for %s: %s", reason, name, e)
            return
        text = (text or "").strip()
        if not text:
            return
        transcripts.append_line(self.guild_id, text, speaker=name, fallback_path=self.transcript_path)
        sec = mono16.size / SR_OUT
        if sec >= MIN_SECONDS_TO_POST:
            try:
                await self.post_channel.send(f"**{name}:** {text}")
            except Exception as e:
                logger.exception("Failed to post transcript: %s", e)

    async def _worker(self) -> None:
        chunk = int(CHUNK_SECONDS * SR_OUT)
        cap = int(MAX_BUFFER_SECONDS * SR_OUT)
        while self._running.is_set():
            await asyncio.sleep(0.3)
            with self._lock:
                keys = list(self._buffers.keys())
            now = time.time()
            for key in keys:
                with self._lock:
                    dq = self._buffers.get(key)
                    name = self._names.get(key, "unknown")
                    last = self._last_activity.get(key, 0.0)
                    total = sum(len(x) for x in dq) if dq else 0
                if not dq or total == 0:
                    continue
                idle = (now - last) >= IDLE_FLUSH_SECONDS
                over_chunk = total >= chunk
                over_cap = total >= cap
                if idle or over_cap:
                    await self._flush_speaker(key, reason="idle" if idle else "cap")
                    continue
                if over_chunk:
                    with self._lock:
                        samples: List[np.ndarray] = []
                        have = 0
                        while dq and have < chunk:
                            part = dq.popleft()
                            samples.append(part)
                            have += len(part)
                    if not samples:
                        continue
                    mono16 = np.concatenate(samples)
                    if mono16.size == 0:
                        continue
                    audio_f32 = (mono16.astype(np.float32) / 32768.0)
                    try:
                        loop = asyncio.get_running_loop()
                        text = await loop.run_in_executor(None, _do_transcribe, self.model, audio_f32, LANG)
                    except Exception as e:
                        logger.exception("Transcribe error for %s: %s", name, e)
                        continue
                    text = (text or "").strip()
                    if not text:
                        continue
                    transcripts.append_line(self.guild_id, text, speaker=name, fallback_path=self.transcript_path)
                    sec = mono16.size / SR_OUT
                    if sec >= MIN_SECONDS_TO_POST:
                        try:
                            await self.post_channel.send(f"**{name}:** {text}")
                        except Exception as e:
                            logger.exception("Failed to post transcript: %s", e)
        return

    async def flush_all(self) -> None:
        with self._lock:
            keys = list(self._buffers.keys())
        for key in keys:
            await self._flush_speaker(key, reason="final")

class Session:
    def __init__(self, vc: voice_recv.VoiceRecvClient, sink: TranscribeSink) -> None:  # type: ignore
        self.vc = vc
        self.sink = sink

SESSIONS: Dict[int, Session] = {}

def _has_active_transcript(guild_id: Optional[int]) -> bool:
    if guild_id is None:
        return False
    return guild_id in transcripts._open_files

def _utc_now_str() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

async def setup_hook() -> None:
    @tree.command(name="record", description="Start or continue recording this voice channel and post transcripts.")
    async def record_cmd(interaction: discord.Interaction) -> None:
        await _cmd_record(interaction)

    @tree.command(name="stop", description="Stop recording in this server.")
    async def stop_cmd(interaction: discord.Interaction) -> None:
        await _cmd_stop(interaction)

    asset_cmds = setup_asset_commands(client, tree)
    add_stats_command(tree, asset_cmds)

    @tree.command(name="scene", description="Insert a scene break into the transcript and post a divider.")
    @app_commands.describe(title="Short scene title, e.g., 'Into the Woods'")
    async def scene_cmd(interaction: discord.Interaction, title: str) -> None:
        g = interaction.guild
        if not g:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        if not _has_active_transcript(g.id):
            await interaction.response.send_message("No active transcript session is open. Use `/record` to start one.", ephemeral=True)
            return
        line = f"--- {title.strip()} ---"
        transcripts.append_line(g.id, line)
        await interaction.response.send_message(line, ephemeral=False)

    @tree.command(name="note", description="Add a note line into the transcript without tagging a speaker.")
    @app_commands.describe(text="Note text to insert")
    async def note_cmd(interaction: discord.Interaction, text: str) -> None:
        g = interaction.guild
        if not g:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        if not _has_active_transcript(g.id):
            await interaction.response.send_message("No active transcript session is open. Use `/record` to start one.", ephemeral=True)
            return
        transcripts.append_line(g.id, text)
        await interaction.response.send_message("Note added.", ephemeral=True)

    @tree.command(name="mark", description="Insert a timestamp marker into the transcript.")
    @app_commands.describe(label="Optional label for the marker")
    async def mark_cmd(interaction: discord.Interaction, label: Optional[str] = None) -> None:
        g = interaction.guild
        if not g:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        if not _has_active_transcript(g.id):
            await interaction.response.send_message("No active transcript session is open. Use `/record` to start one.", ephemeral=True)
            return
        stamp = _utc_now_str()
        txt = f"[{stamp}] {label or 'MARK'}"
        transcripts.append_line(g.id, txt)
        await interaction.response.send_message("Mark inserted.", ephemeral=True)

    await tree.sync()
    logger.info("Slash commands synced.")

client.setup_hook = setup_hook

@client.event
async def on_ready() -> None:
    logger.info(f"READY as {client.user}")

async def _cmd_record(interaction: discord.Interaction) -> None:
    g = interaction.guild
    if g is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    state = getattr(interaction.user, "voice", None)
    channel = getattr(state, "channel", None)
    if channel is None or not isinstance(channel, discord.VoiceChannel):
        await interaction.response.send_message("You are not in a voice channel.", ephemeral=True)
        return
    post_channel = _choose_post_channel(interaction)
    if post_channel is None:
        await interaction.response.send_message("I do not have permission to post in any text channel here.", ephemeral=True)
        return
    existing = SESSIONS.get(g.id)
    if existing and existing.vc and existing.vc.is_connected():
        with contextlib.suppress(Exception):
            existing.sink.start_worker()
        await interaction.response.send_message(
            f"Already recording in {channel.name}. Posting to {getattr(post_channel, 'mention', '#text-channel')}.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    transcript_path = transcripts.start_session(g, channel if isinstance(channel, discord.abc.GuildChannel) else None)
    try:
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False)  # type: ignore
    except Exception:
        transcripts.stop_session(g.id)
        await interaction.followup.send("Could not join the voice channel.", ephemeral=True)
        return
    sink = TranscribeSink(loop=asyncio.get_running_loop(), post_channel=post_channel, model=_model, guild_id=g.id, transcript_path=transcript_path, sr_in=SR_IN, sr_out=SR_OUT)
    try:
        vc.listen(sink)
    except Exception:
        with contextlib.suppress(Exception):
            await vc.disconnect(force=False)
        transcripts.stop_session(g.id)
        await interaction.followup.send("Could not start listening to the voice channel.", ephemeral=True)
        return
    sink.start_worker()
    SESSIONS[g.id] = Session(vc, sink)
    await interaction.followup.send(f"Recording in {channel.name} and posting to {getattr(post_channel, 'mention', '#text-channel')}.", ephemeral=True)

async def _cmd_stop(interaction: discord.Interaction) -> None:
    g = interaction.guild
    if g is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    sess = SESSIONS.pop(g.id, None)
    if not sess:
        await interaction.response.send_message("Not recording in this server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    with contextlib.suppress(Exception):
        sess.vc.stop_listening()  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        await sess.sink.stop_accepting()
    with contextlib.suppress(Exception):
        await sess.sink.flush_all()
    with contextlib.suppress(Exception):
        await sess.sink.stop_worker()
        await sess.sink.join_worker(timeout=12.0)
    with contextlib.suppress(Exception):
        await sess.vc.disconnect(force=False)
    path = transcripts.stop_session(g.id)
    ch = _choose_post_channel(interaction)
    if path and path.exists() and ch is not None:
        try:
            await ch.send(content=f"Session transcript: **{path.name}**", file=discord.File(path, filename=path.name))
        except Exception:
            pass
    await interaction.followup.send("Stopped recording and left the voice channel.", ephemeral=True)

def main() -> None:
    try:
        client.run(TOKEN, log_handler=None)
    except KeyboardInterrupt:
        print("KeyboardInterrupt: shutting down.")

if __name__ == "__main__":
    main()
