# probe_voice.py
import os
import asyncio
import logging
import discord
from dotenv import load_dotenv

# optional but helpful logs
try:
    # exists in discord.py (not py-cord)
    discord.utils.setup_logging(level=logging.DEBUG)
except Exception:
    logging.basicConfig(level=logging.INFO)

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID"))

intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True

class Probe(discord.Client):
    async def on_ready(self):
        print(f"READY as {self.user} ({self.user.id})")
        guild = self.get_guild(GUILD_ID) or await self.fetch_guild(GUILD_ID)
        chan = guild.get_channel(VOICE_CHANNEL_ID)

        if not isinstance(chan, discord.VoiceChannel):
            print("VOICE_CHANNEL_ID is not a voice channel.")
            await self.close()
            return

        print("connecting to voice...")
        try:
            vc = await chan.connect(timeout=15, reconnect=False)
            print("connected:", vc is not None and vc.is_connected())
            await asyncio.sleep(2)  # brief hold so we see the full handshake
            print("disconnecting...")
            await vc.disconnect()
        except Exception as e:
            print("voice connect error:", repr(e))
        finally:
            await self.close()

async def main():
    client = Probe(intents=intents)
    async with client:
        await client.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
