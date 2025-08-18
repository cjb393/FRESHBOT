# asset_commands.py
"""
Campaign Asset Search Module for FreshBot
Provides fuzzy search commands for art and D&D maps with file caching
"""

import asyncio
import json
import hashlib
from typing import List, Tuple, Optional, Dict
from pathlib import Path
from difflib import SequenceMatcher
from datetime import datetime, timezone

import discord
from discord import app_commands


class AssetCache:
    """Handles caching of file metadata for fast searches"""

    def __init__(self, cache_file: str = "asset_cache.json"):
        self.cache_file = Path(cache_file)
        self.cache_data: Dict = {}
        self.load_cache()

    def load_cache(self):
        """Load cache from disk"""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self.cache_data = json.load(f)
        except Exception:
            self.cache_data = {"version": "1.0", "folders": {}}

    def save_cache(self):
        """Save cache to disk"""
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.cache_data, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not save asset cache: {e}")

    def get_folder_hash(self, folder: Path) -> str:
        """Create a hash representing the folder's current state"""
        if not folder.exists():
            return "empty"

        file_info: List[str] = []
        try:
            for file_path in folder.rglob("*"):
                if file_path.is_file():
                    stat = file_path.stat()
                    file_info.append(
                        f"{file_path.relative_to(folder)}:{stat.st_mtime}:{stat.st_size}"
                    )
        except Exception:
            return "error"

        content = "\n".join(sorted(file_info))
        return hashlib.md5(content.encode()).hexdigest()

    def is_folder_cached(self, folder: Path) -> bool:
        """Check if folder cache is still valid"""
        if "folders" not in self.cache_data:
            return False

        folder_str = str(folder)
        if folder_str not in self.cache_data["folders"]:
            return False

        cached_hash = self.cache_data["folders"][folder_str].get("hash", "")
        current_hash = self.get_folder_hash(folder)
        return cached_hash == current_hash

    def get_cached_files(self, folder: Path) -> List[Tuple[str, str]]:
        """Get cached file list for folder"""
        folder_str = str(folder)
        if folder_str in self.cache_data.get("folders", {}):
            return self.cache_data["folders"][folder_str].get("files", [])
        return []

    def cache_folder(self, folder: Path, files: List[Tuple[str, str]]):
        """Cache the file list for a folder"""
        if "folders" not in self.cache_data:
            self.cache_data["folders"] = {}

        folder_str = str(folder)
        self.cache_data["folders"][folder_str] = {
            "hash": self.get_folder_hash(folder),
            "files": files,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save_cache()


class AssetView(discord.ui.View):
    """Interactive view for selecting from multiple asset matches"""

    def __init__(self, assets: List[Tuple[str, Path]], search_term: str, asset_type: str):
        super().__init__(timeout=60.0)
        self.assets = assets[:25]  # Discord limit on select options
        self.search_term = search_term
        self.asset_type = asset_type
        self.selected_file: Optional[Path] = None

        options = []
        for i, (name, path) in enumerate(self.assets):
            display_name = name.replace("_", " ").replace("-", " ")
            if len(display_name) > 100:
                display_name = display_name[:97] + "..."

            options.append(
                discord.SelectOption(
                    label=display_name,
                    value=str(i),
                    description=f"File: {path.name}",
                )
            )

        self.asset_select = discord.ui.Select(
            placeholder=f"Choose a {asset_type} (found {len(assets)} matches)...",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.asset_select.callback = self.on_select
        self.add_item(self.asset_select)

    async def on_select(self, interaction: discord.Interaction):
        """Handle asset selection"""
        try:
            await interaction.response.defer()

            index = int(self.asset_select.values[0])
            name, path = self.assets[index]

            if not path.exists():
                await interaction.edit_original_response(
                    content=f"❌ File not found: **{name}**", view=None
                )
                return

            # Discord free limit ~10 MB; keep headroom
            file_size = path.stat().st_size
            max_size = 10 * 1024 * 1024

            if file_size > max_size:
                size_mb = file_size / (1024 * 1024)
                await interaction.edit_original_response(
                    content=(
                        f"❌ **{name}** is too large to upload\n"
                        f"📁 File size: {size_mb:.1f}MB (limit: 10MB)\n"
                        f"💡 Try a smaller version."
                    ),
                    view=None,
                )
                return

            try:
                discord_file = discord.File(path, filename=path.name)
                await interaction.edit_original_response(
                    content=f"🎨 **{name}**", attachments=[discord_file], view=None
                )
            except discord.HTTPException as e:
                if "413" in str(e) or "too large" in str(e).lower():
                    size_mb = file_size / (1024 * 1024)
                    await interaction.edit_original_response(
                        content=(
                            f"❌ **{name}** failed to upload\n"
                            f"📁 File size: {size_mb:.1f}MB\n"
                            f"💡 Discord rejected the file - try a smaller version."
                        ),
                        view=None,
                    )
                else:
                    await interaction.edit_original_response(
                        content=f"❌ Upload failed: {str(e)}", view=None
                    )

        except Exception as e:
            try:
                await interaction.edit_original_response(
                    content=f"❌ Error loading asset: {str(e)}", view=None
                )
            except Exception:
                await interaction.followup.send(f"❌ Error loading asset: {str(e)}")

    async def on_timeout(self):
        """Called when the view times out"""
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore[attr-defined]


class AssetCommands:
    """Asset search functionality for the Discord bot"""

    SUPPORTED_EXTENSIONS = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".pdf",
        ".svg",
    }

    def __init__(self, bot_client: discord.Client, command_tree: app_commands.CommandTree):
        self.client = bot_client
        self.tree = command_tree
        self.cache = AssetCache()

        self.art_folder = Path("art")
        self.maps_folder = Path("dnd_maps")

        self.art_folder.mkdir(exist_ok=True)
        self.maps_folder.mkdir(exist_ok=True)

        self._cache_ready = asyncio.Event()
        asyncio.create_task(self._initialize_cache())

        self.register_commands()

    async def _initialize_cache(self):
        """Initialize cache in background to avoid blocking startup"""
        try:
            print("🔄 Initializing asset cache...")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._build_cache_if_needed, self.art_folder)
            await loop.run_in_executor(None, self._build_cache_if_needed, self.maps_folder)
            self._cache_ready.set()
            print("✅ Asset cache ready!")
        except Exception as e:
            print(f"❌ Cache initialization failed: {e}")
            self._cache_ready.set()

    def _build_cache_if_needed(self, folder: Path):
        """Build cache for folder if not already cached"""
        if self.cache.is_folder_cached(folder):
            return

        print(f"📁 Scanning {folder.name} folder...")
        files: List[Tuple[str, str]] = []

        if folder.exists():
            for file_path in folder.rglob("*"):
                if not file_path.is_file():
                    continue

                ext = file_path.suffix.lower()
                if ext not in self.SUPPORTED_EXTENSIONS:
                    continue

                rel_obj = file_path.relative_to(folder)
                rel_path = str(rel_obj)

                # Build display name: parents under root + base name (+ [PDF])
                parent_parts = list(rel_obj.parts[:-1])
                base_name = file_path.stem.replace("_", " ").replace("-", " ").strip()
                if ext == ".pdf":
                    base_name = f"{base_name} [PDF]"
                display_name = base_name.title()
                if parent_parts:
                    display_name = f"{'/'.join(parent_parts)} / {display_name}"

                files.append((display_name, rel_path))

        self.cache.cache_folder(folder, files)
        print(f"✅ Cached {len(files)} files from {folder.name}")

    def register_commands(self):
        """Register the slash commands"""

        @self.tree.command(name="art", description="Search for campaign art")
        @app_commands.describe(query="Search term for art (e.g., 'dragon', 'tavern', 'npc')")
        async def art_command(interaction: discord.Interaction, query: str):
            await self._search_assets(interaction, query, "art", self.art_folder)

        @self.tree.command(name="map", description="Search for D&D maps")
        @app_commands.describe(query="Search term for maps (e.g., 'forest', 'dungeon', 'city')")
        async def map_command(interaction: discord.Interaction, query: str):
            await self._search_assets(interaction, query, "map", self.maps_folder)

        @self.tree.command(name="refresh_cache", description="Refresh the asset cache (admin only)")
        async def refresh_command(interaction: discord.Interaction):
            await self._refresh_cache(interaction)

    def _similarity_score(self, text1: str, text2: str) -> float:
        """Calculate similarity between two strings"""
        return SequenceMatcher(None, text1.lower(), text2.lower()).ratio()

    def _fuzzy_search(self, query: str, folder: Path) -> List[Tuple[str, Path]]:
        """Perform fuzzy search using cached data"""
        query = query.lower().strip()
        matches: List[Tuple[float, str, Path]] = []

        cached_files = self.cache.get_cached_files(folder)

        for display_name, rel_path in cached_files:
            full_path = folder / rel_path

            searchable_text = display_name.lower()
            filename_no_ext = Path(rel_path).stem.lower()

            filename_score = self._similarity_score(query, filename_no_ext)
            full_score = self._similarity_score(query, searchable_text)

            query_words = query.split()
            searchable_words = searchable_text.split()
            word_matches = sum(1 for qw in query_words if any(qw in sw for sw in searchable_words))
            word_bonus = word_matches / len(query_words) if query_words else 0

            combined_score = max(filename_score, full_score) + (word_bonus * 0.3)

            if combined_score > 0.3 or any(qw in searchable_text for qw in query_words):
                matches.append((combined_score, display_name, full_path))

        matches.sort(key=lambda x: x[0], reverse=True)
        return [(name, path) for _, name, path in matches[:50]]

    async def _search_assets(
        self, interaction: discord.Interaction, query: str, asset_type: str, folder: Path
    ):
        """Handle asset search command"""
        await interaction.response.defer(thinking=True)

        try:
            if not self.cache.is_folder_cached(folder):
                await interaction.followup.send(
                    "🔄 Cache is outdated, rebuilding... Try again in a moment."
                )
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._build_cache_if_needed, folder)
                return

            matches = self._fuzzy_search(query, folder)

            if not matches:
                await interaction.followup.send(
                    f"❌ No {asset_type} found matching '{query}'. "
                    f"Make sure files are in the `{folder.name}/` folder."
                )
                return

            if len(matches) == 1:
                name, path = matches[0]
                if not path.exists():
                    await interaction.followup.send(f"❌ File not found: **{name}**")
                    return

                file_size = path.stat().st_size
                max_size = 10 * 1024 * 1024  # 10 MB

                if file_size > max_size:
                    size_mb = file_size / (1024 * 1024)
                    await interaction.followup.send(
                        f"❌ **{name}** is too large to upload\n"
                        f"📁 File size: {size_mb:.1f}MB (limit: 10MB)"
                    )
                    return

                try:
                    discord_file = discord.File(path, filename=path.name)
                    await interaction.followup.send(f"🎨 **{name}**", file=discord_file)
                except discord.HTTPException as e:
                    if "413" in str(e) or "too large" in str(e).lower():
                        size_mb = file_size / (1024 * 1024)
                        await interaction.followup.send(
                            f"❌ **{name}** failed to upload\n"
                            f"📁 File size: {size_mb:.1f}MB\n"
                            f"💡 Discord rejected the file - try a smaller version."
                        )
                    else:
                        await interaction.followup.send(f"❌ Upload failed: {str(e)}")
            else:
                view = AssetView(matches, query, asset_type)
                await interaction.followup.send(
                    f"Found {len(matches)} {asset_type}(s) matching '{query}':", view=view
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error searching for {asset_type}: {str(e)}")

    async def _refresh_cache(self, interaction: discord.Interaction):
        """Force refresh the asset cache"""
        await interaction.response.defer(ephemeral=True)
        try:
            self.cache.cache_data = {"version": "1.0", "folders": {}}

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._build_cache_if_needed, self.art_folder)
            await loop.run_in_executor(None, self._build_cache_if_needed, self.maps_folder)

            stats = self.get_stats()
            await interaction.followup.send(
                f"✅ Cache refreshed! Found {stats['art_count']} art files and {stats['map_count']} maps.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Error refreshing cache: {str(e)}", ephemeral=True)

    def get_stats(self) -> dict:
        """Get statistics about stored assets using cache"""
        art_files = self.cache.get_cached_files(self.art_folder)
        map_files = self.cache.get_cached_files(self.maps_folder)
        return {
            "art_count": len(art_files),
            "map_count": len(map_files),
            "total_count": len(art_files) + len(map_files),
        }


# Integration function to add to main bot
def setup_asset_commands(client: discord.Client, tree: app_commands.CommandTree) -> AssetCommands:
    """Set up asset commands on the bot"""
    return AssetCommands(client, tree)


# Optional: Add a stats command for debugging
def add_stats_command(tree: app_commands.CommandTree, asset_commands: AssetCommands):
    """Add a stats command to see asset counts"""

    @tree.command(name="assets", description="Show asset collection statistics")
    async def assets_command(interaction: discord.Interaction):
        stats = asset_commands.get_stats()

        embed = discord.Embed(
            title="📁 Asset Collection",
            color=0x5865F2,
            description="Current campaign assets available",
        )
        embed.add_field(name="🎨 Art", value=f"{stats['art_count']} files", inline=True)
        embed.add_field(name="🗺️ Maps", value=f"{stats['map_count']} files", inline=True)
        embed.add_field(name="📊 Total", value=f"{stats['total_count']} files", inline=True)

        embed.set_footer(text="Use /art or /map to search for assets")

        await interaction.response.send_message(embed=embed, ephemeral=True)
