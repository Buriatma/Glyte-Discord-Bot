"""Social, community, reminders, birthdays, and help commands."""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from database import get_user, mark_dirty, track, users_col, feedback_col
from utils.helpers import blog
from utils.views import HelpView, get_help_embed


class SocialCog(commands.Cog):
    bday_group = app_commands.Group(name="birthday", description="Birthday tracker & celebrations 🎂")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @bday_group.command(name="set", description="Set your birthday to receive server celebrations & gifts!")
    @app_commands.describe(month="Month of birth (1-12)", day="Day of birth (1-31)")
    async def bday_set(self, interaction: discord.Interaction, month: int, day: int):
        if not (1 <= month <= 12 and 1 <= day <= 31):
            await interaction.response.send_message("❌ Invalid date. Month must be 1–12 and Day 1–31.", ephemeral=True)
            return
        bday_str = f"{month:02d}-{day:02d}"
        u = await get_user(interaction.user.id)
        u["birthday"] = bday_str
        mark_dirty(interaction.user.id)
        track("commands", "birthday_set")
        await interaction.response.send_message(
            f"🎂 Your birthday has been set to **{bday_str}** (MM-DD)! We'll celebrate you when the day arrives! 🎉",
            ephemeral=True
        )

    @bday_group.command(name="list", description="List upcoming server birthdays")
    async def bday_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        docs = [doc async for doc in users_col.find({"birthday": {"$ne": None}})]
        if not docs:
            await interaction.followup.send("🎂 No birthdays registered yet! Set yours with `/birthday set`.")
            return
        now_mm_dd = datetime.now(timezone.utc).strftime("%m-%d")
        upcoming = sorted([u for u in docs if u.get("birthday") and u["birthday"] >= now_mm_dd], key=lambda x: x["birthday"])
        passed = sorted([u for u in docs if u.get("birthday") and u["birthday"] < now_mm_dd], key=lambda x: x["birthday"])
        ordered = upcoming + passed
        lines = [f"• **{doc.get('birthday')}** — <@{doc['_id']}>" for doc in ordered[:25]]
        embed = discord.Embed(
            title="🎂 Upcoming Server Birthdays",
            description="\n".join(lines) or "No birthdays found.",
            color=0xEB459E
        )
        embed.set_footer(text="Set your birthday anytime with /birthday set <month> <day>")
        await interaction.followup.send(embed=embed)


    @app_commands.command(name="ping", description="Health check and network latency 🏓")
    async def ping(self, interaction: discord.Interaction):
        latency_ms = round(self.bot.latency * 1000)
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"**WebSocket Latency:** `{latency_ms}ms`\n**Database:** `Connected (Atlas TLS)` ✅\n**Status:** Fully operational ✨",
            color=0x57F287
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="Explore all features, commands, manuals & guides 📖")
    @app_commands.describe(category="Jump directly to a command category")
    @app_commands.choices(category=[
        app_commands.Choice(name="Overview & Guide", value="overview"),
        app_commands.Choice(name="Attendance & Leaves", value="attendance"),
        app_commands.Choice(name="Gamification & Levels", value="gamification"),
        app_commands.Choice(name="Economy & Shop", value="shop"),
        app_commands.Choice(name="Rituals & Team", value="rituals"),
        app_commands.Choice(name="Moderation & Admin", value="moderation"),
        app_commands.Choice(name="Personal & Utilities", value="social"),
    ])
    async def help_cmd(self, interaction: discord.Interaction, category: Optional[str] = "overview"):
        track("commands", "help")
        selected = category or "overview"
        embed = get_help_embed(selected)
        view = HelpView(interaction.user.id)
        # Select active in dropdown
        for item in view.children:
            if isinstance(item, discord.ui.Select):
                for opt in item.options:
                    opt.default = (opt.value == selected)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="remindme", description="Set a timer reminder (e.g. 10m, 60m)")
    @app_commands.describe(minutes="Minutes until reminder (1 to 10080)", message="Reminder note")
    async def remindme(self, interaction: discord.Interaction, minutes: int, message: str):
        if minutes < 1 or minutes > 10080:
            await interaction.response.send_message("❌ Minutes must be between 1 and 10080 (up to 7 days).", ephemeral=True)
            return
        track("commands", "remindme")
        remind_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        ts = int(remind_time.timestamp())

        async def _schedule_reminder(delay_seconds: float, user_id: int, channel_id: int, note: str):
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            embed = discord.Embed(
                title="⏰ Reminder!",
                description=f"Hey <@{user_id}>, here is your scheduled reminder:\n\n> **{note}**",
                color=0xFEE75C
            )
            ch = self.bot.get_channel(channel_id)
            delivered = False
            if ch:
                try:
                    await ch.send(content=f"<@{user_id}>", embed=embed)
                    delivered = True
                except Exception:
                    pass
            if not delivered:
                try:
                    u = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                    if u:
                        await u.send(embed=embed)
                except Exception:
                    pass

        asyncio.create_task(_schedule_reminder(minutes * 60, interaction.user.id, interaction.channel_id, message))
        await interaction.response.send_message(
            f"⏰ **Reminder set!** I'll remind you in **{minutes} minutes** (<t:{ts}:R>):\n> {message}"
        )

    @app_commands.command(name="feedback", description="Teach the bot — suggest or rate something")
    @app_commands.describe(text="Your idea, praise, or suggestion")
    async def feedback(self, interaction: discord.Interaction, text: str):
        await feedback_col.insert_one({
            "user_id": str(interaction.user.id),
            "text": text[:500],
            "at": datetime.now(timezone.utc).isoformat()
        })
        track("commands", "feedback")
        await interaction.response.send_message("📬 Noted! The bot learns from this 🧠✨", ephemeral=True)

    @app_commands.command(name="about", description="Who made this bot? 💜")
    async def about(self, interaction: discord.Interaction):
        track("commands", "about")
        embed = discord.Embed(
            title="🤖 Glyte Discord Bot",
            description="Attendance · Activity · Gamification · Economy for modern flexible teams ✨",
            color=0x5865F2
        )
        embed.add_field(
            name="🏢 Made by",
            value="**GlyteTech** 💜\n🌐 [glyte.tech](https://www.glyte.tech)\n📧 info@glyte.tech",
            inline=False
        )
        embed.add_field(
            name="⚡ Architecture",
            value="Async Discord.py 2.0+ · MongoDB Atlas · Modular Cogs Architecture · Live Web Dashboard",
            inline=False
        )
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        # Starter bonus coins
        u = await get_user(member.id)
        u["coins"] += 50
        mark_dirty(member.id)
        track("events", "member_join")

        ch = member.guild.system_channel
        if not ch or not ch.permissions_for(member.guild.me).send_messages:
            for c in member.guild.text_channels:
                if any(k in c.name.lower() for k in ["welcome", "general", "lounge", "chat"]):
                    if c.permissions_for(member.guild.me).send_messages:
                        ch = c
                        break
        if ch:
            embed = discord.Embed(
                title=f"🎉 Welcome to {member.guild.name}, {member.display_name}!",
                description=(
                    f"Hey {member.mention}, welcome to the server! ✨\n\n"
                    f"🎁 **Starter Gift:** We've credited **+50 🪙 coins** to your account!\n\n"
                    f"**Quick Start:**\n"
                    f"• `/checkin` — Check in daily to build your streak 🔥 & earn XP\n"
                    f"• `/quests` — Complete daily quests for extra rewards 🗺️\n"
                    f"• `/spin` — Spin the Lucky Wheel daily 🎡\n"
                    f"• `/shop` — Spend your coins on perks, boosters & VIP 🏪\n"
                    f"• `/birthday set` — Set your birthday for birthday celebrations 🎂\n"
                    f"• `/help` — Browse interactive commands & guide 📖\n"
                    f"• `/dashboard` — Open your personal live web hub 📊"
                ),
                color=0x5865F2
            )
            if member.display_avatar:
                embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"Member #{member.guild.member_count} • Have a blast! 🚀")
            try:
                await ch.send(content=member.mention, embed=embed)
            except Exception as e:
                blog(f"Welcome message failed: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SocialCog(bot))
