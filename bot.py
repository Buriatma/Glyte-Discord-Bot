"""Glyte Discord Bot - Modular Entrypoint and Cog Runner"""
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from config import TOKEN, INTENTS_MEMBERS, INTENTS_MESSAGE_CONTENT, MONGO_DB
from database import (
    mongo, users_col, leaves_col, standups_col, bounties_col, dash_tokens_col
)
from utils.helpers import blog
from dashboard import start_dashboard

# Configure Privileged Gateway Intents
intents = discord.Intents.default()
intents.members = INTENTS_MEMBERS
intents.message_content = INTENTS_MESSAGE_CONTENT

bot = commands.Bot(command_prefix="!", intents=intents)

# Registered Modular Cogs
COGS = [
    "cogs.attendance",
    "cogs.gamification",
    "cogs.shop",
    "cogs.rituals",
    "cogs.social",
    "cogs.moderation",
    "cogs.events",
    "cogs.scheduler",
]


@bot.tree.error
async def _tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        cause = getattr(error, "original", error)
        cause_name = type(cause).__name__
        detail = str(cause)[:250] if str(cause) else cause_name
        cmd_name = interaction.command.name if interaction.command else "?"
        blog(f"❌ /{cmd_name}: {cause_name}: {detail}")
        msg = f"❌ Something broke: `{cause_name}` ({detail[:120]}). Check `docker compose logs bot` 📜"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


@bot.event
async def setup_hook():
    # Load all modular Cogs
    for ext in COGS:
        try:
            await bot.load_extension(ext)
            blog(f"📦 Loaded extension: {ext}")
        except Exception as e:
            blog(f"❌ Failed to load extension {ext}: {e}")

    # Launch the asynchronous web dashboard server
    try:
        await start_dashboard(bot)
    except Exception as e:
        blog(f"❌ Dashboard server failed to start (bot will continue running): {e}")


@bot.event
async def on_ready():
    blog(f"Logged in as {bot.user} ({bot.user.id})")

    # Synchronize Slash Command Tree
    try:
        synced = await bot.tree.sync()
        blog(f"Synced {len(synced)} slash commands")
    except Exception as e:
        blog(f"Sync failed: {e}")

    # Verify MongoDB Atlas Connectivity
    try:
        await mongo.admin.command("ping")
        blog(f"Mongo connected: {MONGO_DB}")
    except Exception as e:
        blog(f"Mongo ping failed: {e}")

    # Ensure MongoDB Collection Indexes
    for idx in ["xp", "coins", "messages", "voice_seconds", "last_checkin"]:
        try:
            await users_col.create_index(idx)
        except Exception:
            pass
    try:
        await leaves_col.create_index([("user_id", 1), ("status", 1)])
    except Exception:
        pass
    for col, keys in [(standups_col, [("user_id", 1), ("date", 1)]), (bounties_col, [("status", 1)])]:
        try:
            await col.create_index(keys)
        except Exception:
            pass
    try:
        await dash_tokens_col.create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        pass

    blog("ready — commands live")


def main():
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is missing! Check your .env file.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
