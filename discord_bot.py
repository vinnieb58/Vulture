"""
discord_bot.py

Discord slash command bot for Vulture v2.0 hunt management.

This is a SEPARATE runtime from main.py.
  main.py      — scheduled hunt execution worker
  discord_bot.py — Discord control surface (create/pause/end hunts)

Start the bot:
    python discord_bot.py

Required environment variable (.env or system):
    DISCORD_BOT_TOKEN   — bot token from the Discord Developer Portal

Optional environment variable:
    DISCORD_GUILD_ID    — integer guild (server) ID for instant slash command
                          registration during development.
                          If omitted, commands are registered globally
                          (can take up to one hour to propagate).
"""

import logging
import os
import sys
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from engine.command_router import dispatch
from engine.database import init_db
from engine.hunt_repository import init_hunts_table

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/vulture.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    log.error("DISCORD_BOT_TOKEN is not set. Add it to your .env file.")
    sys.exit(1)

_raw_guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
GUILD = discord.Object(id=int(_raw_guild_id)) if _raw_guild_id.isdigit() else None

# Discord limits one message to 2000 characters; leave room for truncation notice.
_MAX_MSG = 1900


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_csv(value: str | None) -> list[str]:
    """Split a comma-separated string into a clean list. Returns [] for None."""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _reply_text(result) -> str:
    """Return result.message, truncated to Discord's message limit if needed."""
    text = result.message
    if len(text) > _MAX_MSG:
        text = text[:_MAX_MSG] + "\n…*(truncated)*"
    return text


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class VultureBot(discord.Client):
    """
    Minimal Discord client for Vulture.

    Uses discord.Client directly (no ext.commands overhead) since all
    commands are slash commands registered via the app_commands tree.
    """

    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        """Called once before the bot connects. Syncs slash commands."""
        if GUILD:
            # Guild-scoped: registers instantly (use during development)
            self.tree.copy_global_to(guild=GUILD)
            await self.tree.sync(guild=GUILD)
            log.info("Slash commands synced to guild %s", GUILD.id)
        else:
            # Global: takes up to one hour to propagate
            await self.tree.sync()
            log.info("Slash commands synced globally")

    async def on_ready(self) -> None:
        log.info("Vulture bot ready — logged in as %s (id: %s)", self.user, self.user.id)


bot = VultureBot()


# ---------------------------------------------------------------------------
# /hunt_list
# ---------------------------------------------------------------------------

@bot.tree.command(name="hunt_list", description="List all hunts, optionally filtered by status.")
@app_commands.describe(status="Filter by status: active, paused, or ended. Leave blank for all.")
async def hunt_list(
    interaction: discord.Interaction,
    status: str | None = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    result = dispatch("list", {"status": status or None})
    await interaction.followup.send(_reply_text(result), ephemeral=True)


# ---------------------------------------------------------------------------
# /hunt_show
# ---------------------------------------------------------------------------

@bot.tree.command(name="hunt_show", description="Show full details for a hunt.")
@app_commands.describe(hunt_id="The hunt ID (UUID) shown by /hunt_list.")
async def hunt_show(
    interaction: discord.Interaction,
    hunt_id: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    result = dispatch("show", {"hunt_id": hunt_id.strip()})
    await interaction.followup.send(_reply_text(result), ephemeral=True)


# ---------------------------------------------------------------------------
# /hunt_create
# ---------------------------------------------------------------------------

@bot.tree.command(name="hunt_create", description="Create a new hunt.")
@app_commands.describe(
    name            = "Unique name for the hunt, e.g. gpu_houston",
    search_terms    = "Comma-separated search terms, e.g. gpu, graphics card",
    source_sites    = "Comma-separated sources, e.g. craigslist",
    max_price       = "Maximum price in dollars (integer), e.g. 400",
    location        = "City or location string, e.g. houston",
    include_keywords= "Comma-separated keywords that must appear in the title",
    exclude_keywords= "Comma-separated keywords that must NOT appear in the title",
    category        = "Optional category label, e.g. gpu",
    notes           = "Optional free-text notes",
)
async def hunt_create(
    interaction: discord.Interaction,
    name: str,
    search_terms: str,
    source_sites: str,
    max_price: int | None = None,
    location: str | None = None,
    include_keywords: str | None = None,
    exclude_keywords: str | None = None,
    category: str | None = None,
    notes: str | None = None,
) -> None:
    await interaction.response.defer(ephemeral=True)

    result = dispatch("create", {
        "name":             name.strip(),
        "search_terms":     _split_csv(search_terms),
        "source_sites":     _split_csv(source_sites),
        "max_price":        max_price,
        "location":         location,
        "include_keywords": _split_csv(include_keywords),
        "exclude_keywords": _split_csv(exclude_keywords),
        "category":         category,
        "notes":            notes,
        "created_by":       str(interaction.user),
    })
    await interaction.followup.send(_reply_text(result), ephemeral=True)


# ---------------------------------------------------------------------------
# /hunt_pause
# ---------------------------------------------------------------------------

@bot.tree.command(name="hunt_pause", description="Temporarily pause an active hunt.")
@app_commands.describe(hunt_id="The hunt ID to pause.")
async def hunt_pause(
    interaction: discord.Interaction,
    hunt_id: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    result = dispatch("pause", {"hunt_id": hunt_id.strip()})
    await interaction.followup.send(_reply_text(result), ephemeral=True)


# ---------------------------------------------------------------------------
# /hunt_resume
# ---------------------------------------------------------------------------

@bot.tree.command(name="hunt_resume", description="Resume a paused hunt.")
@app_commands.describe(hunt_id="The hunt ID to resume.")
async def hunt_resume(
    interaction: discord.Interaction,
    hunt_id: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    result = dispatch("resume", {"hunt_id": hunt_id.strip()})
    await interaction.followup.send(_reply_text(result), ephemeral=True)


# ---------------------------------------------------------------------------
# /hunt_end
# ---------------------------------------------------------------------------

@bot.tree.command(name="hunt_end", description="Permanently end a hunt (cannot be undone).")
@app_commands.describe(hunt_id="The hunt ID to end.")
async def hunt_end(
    interaction: discord.Interaction,
    hunt_id: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    result = dispatch("end", {"hunt_id": hunt_id.strip()})
    await interaction.followup.send(_reply_text(result), ephemeral=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Initialize the database tables before the bot starts receiving commands.
    # This is safe to call on every startup (CREATE TABLE IF NOT EXISTS).
    init_db()
    init_hunts_table()
    log.info("Starting Vulture Discord bot")
    bot.run(BOT_TOKEN, log_handler=None)  # log_handler=None — we manage our own


if __name__ == "__main__":
    main()
