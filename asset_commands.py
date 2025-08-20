# asset_commands.py
"""
Campaign Asset Search Module for FreshBot
Provides improved fuzzy search and constrained random for art and D&D maps with file caching.
- Supports PNG/JPG/WEBP/BMP/GIF/SVG/PDF discovery
- Auto-rasterizes SVG to PNG before upload (Discord doesn't preview SVG)
- 10 MB upload cap to avoid 413 errors on free servers
- Smarter matching with tokenization, phrase and exclude operators, and edit-distance penalty
- Constrained random commands that pick from on-theme results
"""

import asyncio
import json
import hashlib
import subprocess
import tempfile
import shutil
import contextlib
import random
import re
from typing import List, Tuple, Optional, Dict, Iterable
from pathlib import Path
from datetime import datetime, timezone

import discord
from discord import app_commands


# ---------- utilities ----------

def _rasterize_svg(svg_path: Path) -> Optional[Path]:
    """
    Convert an SVG to a temporary PNG with alpha preserved.
    Requires ImageMagick `magick` on PATH.
    Returns output PNG Path or None on failure.
    """
    if shutil.which("magick") is None:
        return None

    out = Path(tempfile.gettempdir()) / f"{svg_path.stem}.png"
    cmd = [
        "magick",
        str(svg_path),
        "-background", "none",
        "-density", "300",
        "-resize", "2048x2048>",
        str(out),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out if out.exists() else None
    except Exception:
        return None


def _load_synonyms(path: Path = Path("synonyms.json")) -> Dict[str, List[str]]:
    """
    Load optional synonyms. Format:
    {
      "dragon": ["wyrm", "drake"],
      "undead": ["zombie","skeleton","ghoul"]
    }
    """
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                out: Dict[str, List[str]] = {}
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, list):
                        out[k.lower()] = [str(x).lower() for x in v if isinstance(x, (str, int, float))]
                return out
    except Exception:
        pass
    return {}


def _normalize_text(s: str) -> str:
    # Lowercase and spaces for matching
    return re.sub(r"[_\-]+", " ", s).lower()


def _tokenize(s: str) -> List[str]:
    # Split on non-alphanumerics and camelcase boundaries
    s = _normalize_text(s)
    parts = re.split(r"[^a-z0-9]+", s)
    return [p for p in parts if p]


def _levenshtein(a: str, b: str, max_distance: int = 3) -> int:
    """
    Simple Levenshtein distance with an optional early exit when distance exceeds max_distance.
    """
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > max_distance:
        return max_distance + 1
    if la == 0:
        return lb
    if lb == 0:
        return la

    prev = list(range(lb + 1))
    curr = [0] * (lb + 1)
    for i in range(1, la + 1):
        curr[0] = i
        ai = a[i - 1]
        row_min = curr[0]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,       # deletion
                curr[j - 1] + 1,   # insertion
                prev[j - 1] + cost # substitution
            )
            if curr[j] < row_min:
                row_min = curr[j]
        if row_min > max_distance:
            return max_distance + 1
        prev, curr = curr, prev
    return prev[lb]


def _parse_query(query: str) -> Dict[str, Iterable[str]]:
    """
    Parse query with support for:
      - phrases in quotes: "red dragon"
      - excludes: -word
      - regular words
    Returns dict with keys: phrases, include, exclude
    """
    phrases: List[str] = []
    include: List[str] = []
    exclude: List[str] = []

    for m in re.finditer(r'"([^"]+)"', query):
        phrases.append(m.group(1).strip().lower())

    query_wo_phrases = re.sub(r'"[^"]+"', " ", query)
    for tok in query_wo_phrases.split():
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("-") and len(tok) > 1:
            exclude.append(tok[1:].lower())
        else:
            include.append(tok.lower())

    return {"phrases": phrases, "include": include, "exclude": exclude}


class AssetCache:
    """Handles caching of file metadata for fast searches"""

    def __init__(self, cache_file: str = "asset_cache.json"):
        self.cache_file = Path(cache_file)
        self.cache_data: Dict = {}
        self.load_cache()

    def load_cache(self) -> None:
        try:
            if self.cache_file.exists():
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self.cache_data = json.load(f)
        except Exception:
            self.cache_data = {"version": "1.0", "folders": {}}

    def save_cache(self) -> None:
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.cache_data, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not save asset cache: {e}")

    def get_folder_hash(self, folder: Path) -> str:
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
        if "folders" not in self.cache_data:
            return False

        folder_str = str(folder)
        if folder_str not in self.cache_data["folders"]:
            return False

        cached_hash = self.cache_data["folders"][folder_str].get("hash", "")
        current_hash = self.get_folder_hash(folder)
        return cached_hash == current_hash

    def get_cached_files(self, folder: Path) -> List[Tuple[str, str]]:
        folder_str = str(folder)
        if folder_str in self.cache_data.get("folders", {}):
            return self.cache_data["folders"][folder_str].get("files", [])
        return []

    def cache_folder(self, folder: Path, files: List[Tuple[str, str]]) -> None:
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
        self.assets = assets[:25]
        self.search_term = search_term
        self.asset_type = asset_type

        options: List[discord.SelectOption] = []
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

    async def on_select(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer()

            index = int(self.asset_select.values[0])
            name, orig_path = self.assets[index]

            if not orig_path.exists():
                await interaction.edit_original_response(
                    content=f"❌ File not found: **{name}**", view=None
                )
                return

            send_path = orig_path
            cleanup: Optional[Path] = None
            if orig_path.suffix.lower() == ".svg":
                png = _rasterize_svg(orig_path)
                if png and png.exists():
                    send_path = png
                    cleanup = png

            file_size = send_path.stat().st_size
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
                if cleanup:
                    with contextlib.suppress(Exception):
                        cleanup.unlink()
                return

            try:
                fname = send_path.name if send_path.suffix.lower() != ".svg" else f"{send_path.stem}.png"
                discord_file = discord.File(send_path, filename=fname)
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
            finally:
                if cleanup:
                    with contextlib.suppress(Exception):
                        cleanup.unlink()

        except Exception as e:
            try:
                await interaction.edit_original_response(
                    content=f"❌ Error loading asset: {str(e)}", view=None
                )
            except Exception:
                await interaction.followup.send(f"❌ Error loading asset: {str(e)}")

    async def on_timeout(self) -> None:
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
        self.synonyms = _load_synonyms()

        self.art_folder = Path("art")
        self.maps_folder = Path("dnd_maps")

        self.art_folder.mkdir(exist_ok=True)
        self.maps_folder.mkdir(exist_ok=True)

        self._cache_ready = asyncio.Event()
        asyncio.create_task(self._initialize_cache())

        self.register_commands()

    async def _initialize_cache(self) -> None:
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

    def _build_cache_if_needed(self, folder: Path) -> None:
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

    # ---------- Improved search ----------

    def _expand_with_synonyms(self, terms: List[str]) -> List[str]:
        out: List[str] = []
        for t in terms:
            out.append(t)
            if t in self.synonyms:
                out.extend(self.synonyms[t])
        seen: set = set()
        uniq: List[str] = []
        for t in out:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return uniq

    def _match_score(
        self,
        query_include: List[str],
        query_phrases: List[str],
        excludes: List[str],
        display_name: str,
        rel_path: str,
    ) -> float:
        dn_norm = _normalize_text(display_name)
        rp_norm = _normalize_text(rel_path)
        searchable = f"{dn_norm} {rp_norm}"
        tokens = set(_tokenize(searchable))

        for ex in excludes:
            ex_norm = ex.lower()
            if ex_norm in tokens or f" {ex_norm} " in f" {searchable} ":
                return 0.0

        score = 0.0

        for ph in query_phrases:
            ph_norm = _normalize_text(ph)
            if ph_norm and ph_norm in searchable:
                score += 1.5

        for term in query_include:
            term_norm = term.lower()
            term_score = 0.0

            if term_norm in tokens:
                term_score = 1.0
            else:
                if any(t.startswith(term_norm) for t in tokens):
                    term_score = 0.75
                elif term_norm in searchable:
                    term_score = 0.5
                else:
                    closest = min((_levenshtein(term_norm, t, 3) for t in tokens), default=4)
                    if closest == 1:
                        term_score = 0.35
                    elif closest == 2:
                        term_score = 0.15
                    elif closest <= 3:
                        term_score = 0.05
                    else:
                        term_score = 0.0

            score += term_score

        parent_parts = _normalize_text("/".join(rel_path.split("/")[:-1]))
        if parent_parts:
            for term in query_include:
                t = term.lower()
                if t and t in parent_parts:
                    score += 0.15

        return score

    def _filter_and_rank(
        self,
        query: str,
        folder: Path,
        limit: int = 12,
        min_score: float = 0.30,
    ) -> List[Tuple[str, Path]]:
        parsed = _parse_query(query)
        phrases = list(parsed["phrases"])
        include = list(parsed["include"])
        exclude = list(parsed["exclude"])

        include = self._expand_with_synonyms(include)

        results: List[Tuple[float, str, Path]] = []
        cached_files = self.cache.get_cached_files(folder)

        for display_name, rel_path in cached_files:
            full_path = folder / rel_path
            if not full_path.exists():
                continue

            s = self._match_score(include, phrases, exclude, display_name, rel_path)
            if s >= min_score:
                results.append((s, display_name, full_path))

        results.sort(key=lambda x: x[0], reverse=True)
        top = results[: max(1, min(limit, 50))]
        return [(name, path) for _, name, path in top]

    # ---------- Commands ----------

    def register_commands(self) -> None:
        @self.tree.command(name="art", description="Search for campaign art")
        @app_commands.describe(
            query="Search term for art (supports quotes and excludes: ex. \"red dragon\" -desert)",
            limit="Max results to show (default 12, up to 50)",
        )
        async def art_command(interaction: discord.Interaction, query: str, limit: Optional[int] = 12) -> None:
            await self._search_assets(interaction, query, "art", self.art_folder, limit)

        @self.tree.command(name="map", description="Search for D&D maps")
        @app_commands.describe(
            query="Search term for maps (supports quotes and excludes: ex. \"large dungeon\" -lava)",
            limit="Max results to show (default 12, up to 50)",
        )
        async def map_command(interaction: discord.Interaction, query: str, limit: Optional[int] = 12) -> None:
            await self._search_assets(interaction, query, "map", self.maps_folder, limit)

        @self.tree.command(name="art_random", description="Pick a random art asset that matches your query")
        @app_commands.describe(query="On-theme random, ex. dragon, tavern, npc portrait")
        async def art_random_command(interaction: discord.Interaction, query: str) -> None:
            await self._random_asset(interaction, query, "art", self.art_folder)

        @self.tree.command(name="map_random", description="Pick a random map that matches your query")
        @app_commands.describe(query="On-theme random, ex. large dungeon, forest, village night")
        async def map_random_command(interaction: discord.Interaction, query: str) -> None:
            await self._random_asset(interaction, query, "map", self.maps_folder)

        @self.tree.command(name="refresh_cache", description="Refresh the asset cache (admin only)")
        async def refresh_command(interaction: discord.Interaction) -> None:
            await self._refresh_cache(interaction)

    async def _search_assets(
        self,
        interaction: discord.Interaction,
        query: str,
        asset_type: str,
        folder: Path,
        limit: Optional[int] = 12,
    ) -> None:
        await interaction.response.defer(thinking=True)

        try:
            lim = 12 if limit is None else max(1, min(int(limit), 50))

            if not self.cache.is_folder_cached(folder):
                await interaction.followup.send(
                    "🔄 Cache is outdated, rebuilding... Try again in a moment."
                )
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._build_cache_if_needed, folder)
                return

            matches = self._filter_and_rank(query, folder, limit=lim, min_score=0.30)

            if not matches:
                await interaction.followup.send(
                    f"❌ No {asset_type} found matching '{query}'. "
                    f"Try a simpler term or different wording."
                )
                return

            if len(matches) == 1:
                await self._send_single_asset(interaction, matches[0], asset_type)
            else:
                view = AssetView(matches, query, asset_type)
                await interaction.followup.send(
                    f"Found {len(matches)} {asset_type}(s) matching '{query}':", view=view
                )

        except Exception as e:
            await interaction.followup.send(f"❌ Error searching for {asset_type}: {str(e)}")

    async def _random_asset(
        self,
        interaction: discord.Interaction,
        query: str,
        asset_type: str,
        folder: Path,
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            if not self.cache.is_folder_cached(folder):
                await interaction.followup.send(
                    "🔄 Cache is outdated, rebuilding... Try again in a moment."
                )
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._build_cache_if_needed, folder)
                return

            filtered = self._filter_and_rank(query, folder, limit=50, min_score=0.62)
            if not filtered:
                await interaction.followup.send(
                    f"❌ No {asset_type} matched strongly enough for random pick. "
                    f"Try a different term or be more specific."
                )
                return

            choice = random.choice(filtered)
            await self._send_single_asset(interaction, choice, asset_type)

        except Exception as e:
            await interaction.followup.send(f"❌ Error selecting random {asset_type}: {str(e)}")

    async def _send_single_asset(
        self,
        interaction: discord.Interaction,
        item: Tuple[str, Path],
        asset_type: str,
    ) -> None:
        name, orig_path = item
        if not orig_path.exists():
            await interaction.followup.send(f"❌ File not found: **{name}**")
            return

        send_path = orig_path
        cleanup: Optional[Path] = None
        if orig_path.suffix.lower() == ".svg":
            png = _rasterize_svg(orig_path)
            if png and png.exists():
                send_path = png
                cleanup = png

        file_size = send_path.stat().st_size
        max_size = 10 * 1024 * 1024

        if file_size > max_size:
            size_mb = file_size / (1024 * 1024)
            await interaction.followup.send(
                f"❌ **{name}** is too large to upload\n"
                f"📁 File size: {size_mb:.1f}MB (limit: 10MB)"
            )
            if cleanup:
                with contextlib.suppress(Exception):
                    cleanup.unlink()
            return

        try:
            fname = send_path.name if send_path.suffix.lower() != ".svg" else f"{send_path.stem}.png"
            discord_file = discord.File(send_path, filename=fname)
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
        finally:
            if cleanup:
                with contextlib.suppress(Exception):
                    cleanup.unlink()

    async def _refresh_cache(self, interaction: discord.Interaction) -> None:
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
        art_files = self.cache.get_cached_files(self.art_folder)
        map_files = self.cache.get_cached_files(self.maps_folder)
        return {
            "art_count": len(art_files),
            "map_count": len(map_files),
            "total_count": len(art_files) + len(map_files),
        }


def setup_asset_commands(client: discord.Client, tree: app_commands.CommandTree) -> AssetCommands:
    return AssetCommands(client, tree)


def add_stats_command(tree: app_commands.CommandTree, asset_commands: AssetCommands) -> None:
    @tree.command(name="assets", description="Show asset collection statistics")
    async def assets_command(interaction: discord.Interaction) -> None:
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
