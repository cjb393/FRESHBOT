# app.py
import os
import logging
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

# ---------- Config ----------
load_dotenv()
logging.basicConfig(level=logging.INFO)

TOKEN: str = os.getenv("DISCORD_TOKEN", "") or ""
GUILD_ID: int = int(os.getenv("GUILD_ID", "0"))

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in your .env")
if GUILD_ID == 0:
    raise RuntimeError("GUILD_ID is not set in your .env")

# ---------- Client / Intents ----------
intents = discord.Intents.default()
intents.voice_states = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Guild object for guild-scoped commands (instant availability)
MY_GUILD = discord.Object(id=GUILD_ID)


# ---------- Events ----------
@client.event
async def on_ready() -> None:
    print(f"READY as {client.user}")
    # Force-sync commands to the one guild so they appear immediately
    try:
        await tree.sync(guild=MY_GUILD)
        print(f"Slash commands force-synced to guild {GUILD_ID}")
    except Exception as e:
        print("Slash sync failed:", e)


# ---------- Commands ----------
@tree.command(
    name="join",
    description="Join the voice channel you're currently in.",
    guild=MY_GUILD,
)
async def join(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    # Where is the user?
    user_state = getattr(interaction.user, "voice", None)
    user_channel = getattr(user_state, "channel", None)
    if user_channel is None:
        await interaction.response.send_message(
            "You're not in a voice channel.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    # Current bot voice connection (if any), typed for Pylance
    vc_proto = guild.voice_client
    vc: Optional[discord.VoiceClient] = (
        vc_proto if isinstance(vc_proto, discord.VoiceClient) else None
    )

    if vc is None:
        # Not connected yet -> connect
        await user_channel.connect(self_deaf=True)
        await interaction.followup.send(
            f"Joined **{user_channel.name}**.", ephemeral=True
        )
    else:
        # Already connected -> move
        await vc.move_to(user_channel)
        await interaction.followup.send(
            f"Moved to **{user_channel.name}**.", ephemeral=True
        )


@tree.command(
    name="leave",
    description="Leave the current voice channel.",
    guild=MY_GUILD,
)
async def leave(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    vc_proto = guild.voice_client
    vc: Optional[discord.VoiceClient] = (
        vc_proto if isinstance(vc_proto, discord.VoiceClient) else None
    )
    if vc is None:
        await interaction.response.send_message(
            "I'm not in a voice channel.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    await vc.disconnect(force=False)
    await interaction.followup.send("Left the voice channel.", ephemeral=True)


# ---------- Run ----------
client.run(TOKEN)
