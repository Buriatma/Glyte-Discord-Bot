"""Gamification cog: quests, seasons, leaderboards, lucky wheel, and pomodoro focus."""
import asyncio
import time
import random
from datetime import datetime, timezone
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from config import QUESTS, today_str, season_str
from database import (
    users_col, get_user, mark_dirty, track
)
from utils.helpers import (
    calc_level, fmt_duration, bump_season, grant_badges, apply_level_roles
)
from utils.rituals import eff_targets
from utils.views import FocusControlView


def quest_progress(u: dict, day: str) -> dict:
    d = u.get("daily", {}).get(day, {})
    return {
        "checkin": 1 if day in u.get("checkins", []) else 0,
        "chatter": d.get("messages", 0),
        "voicer": d.get("voice", 0),
        "reactor": d.get("reactions", 0),
    }


class GamificationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="quests", description="Show today's quests and progress")
    async def quests(self, interaction: discord.Interaction):
        await interaction.response.defer()
        u = await get_user(interaction.user.id)
        day = today_str()
        targets = await eff_targets()
        prog = quest_progress(u, day)
        claimed = u.get("quest_claimed", {})
        embed = discord.Embed(title=f"🗺️ Quests for {day}", color=0x5865F2)
        for qid, q in QUESTS.items():
            done = prog.get(qid, 0)
            need = targets[qid]
            is_claimed = claimed.get(qid) == day
            is_ready = done >= need and not is_claimed
            status = "✅ Claimed" if is_claimed else ("🎁 Ready! `/quest_claim " + qid + "`" if is_ready else "⏳ In progress")
            display_done = fmt_duration(done) if qid == "voicer" else str(done)
            display_need = fmt_duration(need) if qid == "voicer" else str(need)
            embed.add_field(
                name=f"{q['name']} — +{q['xp']} XP, +{q['coins']} 🪙",
                value=f"{q['desc']}\n`{display_done}/{display_need}` · **{status}**",
                inline=False
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="quest_claim", description="Claim completed quest rewards")
    @app_commands.describe(quest_id="Quest ID (e.g. checkin, chatter, voicer, reactor)")
    async def quest_claim(self, interaction: discord.Interaction, quest_id: str):
        quest_id = quest_id.lower().strip()
        if quest_id not in QUESTS:
            await interaction.response.send_message("❌ Unknown quest. Use `/quests` to see IDs.", ephemeral=True)
            return
        u = await get_user(interaction.user.id)
        day = today_str()
        if u.get("quest_claimed", {}).get(quest_id) == day:
            await interaction.response.send_message("Already claimed today.", ephemeral=True)
            return
        targets = await eff_targets()
        prog = quest_progress(u, day)
        q = QUESTS[quest_id]
        if prog.get(quest_id, 0) < targets[quest_id]:
            await interaction.response.send_message(
                f"Not done yet: {prog.get(quest_id, 0)}/{targets[quest_id]}", ephemeral=True)
            return
        old_lvl = calc_level(u["xp"])
        u["xp"] += q["xp"]
        u["coins"] += q["coins"]
        bump_season(u, xp=q["xp"])
        u["quest_claimed"][quest_id] = day
        grant_badges(u)
        mark_dirty(interaction.user.id)
        track("quests", quest_id)
        track("commands", "quest_claim")
        new_lvl = calc_level(u["xp"])
        if new_lvl > old_lvl:
            await apply_level_roles(interaction.user, new_lvl)
        await interaction.response.send_message(
            f"🎉 Quest **{q['name']}** claimed! +{q['xp']} XP, +{q['coins']} 🪙")

    @app_commands.command(name="leaderboard", description="Top members by XP, coins, messages, voice, check-ins")
    @app_commands.describe(category="What to rank by", limit="How many (max 25)")
    @app_commands.choices(category=[
        app_commands.Choice(name="XP", value="xp"),
        app_commands.Choice(name="Coins", value="coins"),
        app_commands.Choice(name="Messages", value="messages"),
        app_commands.Choice(name="Voice time", value="voice_seconds"),
        app_commands.Choice(name="Check-ins", value="checkins"),
    ])
    async def leaderboard(self, interaction: discord.Interaction,
                          category: Optional[app_commands.Choice[str]] = None,
                          limit: int = 10):
        await interaction.response.defer()
        key = category.value if category else "xp"
        limit = max(1, min(limit, 25))
        guild = interaction.guild
        if key == "checkins":
            cursor = users_col.aggregate([
                {"$project": {"score": {"$size": {"$ifNull": ["$checkins", []]}}}},
                {"$sort": {"score": -1}}, {"$limit": limit}])
            rows = [(d["_id"], d["score"]) async for d in cursor]
        else:
            cursor = users_col.find({}, {"_id": 1, key: 1}).sort(key, -1).limit(limit)
            rows = [(d["_id"], d.get(key, 0)) async for d in cursor]
        lines = []
        for i, (uid, score) in enumerate(rows, 1):
            name = f"<@{uid}>"
            if guild:
                try:
                    m = guild.get_member(int(uid))
                    if m:
                        name = m.display_name
                except ValueError:
                    pass
            s = fmt_duration(score) if key == "voice_seconds" else str(score)
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`{i}.`")
            lines.append(f"{medal} **{name}** — {s}")
        label = category.name if category else "XP"
        await interaction.followup.send(embed=discord.Embed(
            title=f"🏆 Leaderboard — {label}",
            description="\n".join(lines) if lines else "No data yet.", color=0xFEE75C))

    @app_commands.command(name="season", description="Show the monthly season leaderboard")
    @app_commands.describe(month="YYYY-MM (default: current)")
    async def season(self, interaction: discord.Interaction, month: Optional[str] = None):
        await interaction.response.defer()
        m = month or season_str()
        cursor = users_col.find({f"seasons.{m}.xp": {"$gt": 0}}).sort(f"seasons.{m}.xp", -1).limit(10)
        rows = [d async for d in cursor]
        lines = []
        for i, d in enumerate(rows, 1):
            s = d.get("seasons", {}).get(m, {})
            name = f"<@{d['_id']}>"
            if interaction.guild:
                try:
                    mem = interaction.guild.get_member(int(d["_id"]))
                    if mem:
                        name = mem.display_name
                except ValueError:
                    pass
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`{i}.`")
            lines.append(f"{medal} **{name}** — **{s.get('xp', 0)} XP** · "
                         f"{s.get('messages', 0)} msgs · {fmt_duration(s.get('voice', 0))}")
        embed = discord.Embed(title=f"🏁 Season {m} Leaderboard",
                              description="\n".join(lines) or "No activity yet this season.",
                              color=0xEB459E)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="badges", description="Show badges")
    @app_commands.describe(member="Member (default: you)")
    async def badges(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer()
        member = member or interaction.user
        u = await get_user(member.id)
        await interaction.followup.send(embed=discord.Embed(
            title=f"🏅 {member.display_name}'s Badges",
            description=", ".join(f"`{b}`" for b in u.get("badges", [])) or "No badges yet.",
            color=0xFEE75C))

    @app_commands.command(name="spin", description="Spin the Lucky Wheel for daily rewards & badges")
    async def spin(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uid = str(interaction.user.id)
        u = await get_user(uid)
        today = today_str()
        cost = 25
        is_free = (u.get("last_spin") != today)

        if not is_free:
            if u.get("coins", 0) < cost:
                await interaction.followup.send(
                    f"❌ You've already used your free spin today! Extra spins cost **{cost} coins**, but you have **{u.get('coins', 0)} coins**.",
                    ephemeral=True
                )
                return
            u["coins"] -= cost
        else:
            u["last_spin"] = today

        prizes = [
            ("🪙 50 Coins", 50, 0, None, 28),
            ("🪙 120 Coins", 120, 0, None, 18),
            ("🪙 250 Coins", 250, 0, None, 10),
            ("✨ +40 XP", 0, 40, None, 20),
            ("✨ +100 XP", 0, 100, None, 12),
            ("🏅 Mystery Badge + 100 Coins", 100, 20, "lucky-spinner", 8),
            ("💥 JACKPOT! 777 Coins + 200 XP", 777, 200, "jackpot-king", 4),
        ]
        weights = [p[4] for p in prizes]
        chosen = random.choices(prizes, weights=weights, k=1)[0]
        label, p_coins, p_xp, p_badge, _ = chosen

        u["coins"] += p_coins
        u["xp"] += p_xp
        bump_season(u, xp=p_xp)
        new_badge = False
        if p_badge:
            b_list = set(u.get("badges", []))
            if p_badge not in b_list:
                b_list.add(p_badge)
                new_badge = True
            u["badges"] = sorted(b_list)

        mark_dirty(uid)
        track("commands", "spin")

        fee_text = "🎁 **Free Daily Spin!**" if is_free else f"💸 Used **{cost} coins** for an extra spin."
        is_jackpot = "JACKPOT" in label
        embed = discord.Embed(
            title="🎰 Glyte Lucky Wheel 🎡",
            color=0xEB459E if is_jackpot else 0xFEE75C,
            description=f"{fee_text}\n\n"
                        f"The wheel spins...\n"
                        f"**[ 🎡 ✦ ✦ ✦ 🎡 ]**\n\n"
                        f"🎯 **Outcome:**\n# {label}\n\n"
                        f"💰 New Balance: **{u['coins']}** 🪙\n"
                        f"✨ Total XP: **{u['xp']}**"
        )
        if new_badge:
            embed.add_field(name="🏅 New Badge Unlocked!", value=f"`{p_badge}`", inline=False)
        embed.set_footer(text="1 free spin every day · Extra spins 25 coins")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="focus", description="Start a Pomodoro deep work sprint with live rewards")
    @app_commands.describe(minutes="Duration in minutes (5 to 120)", task="What are you working on?")
    async def focus(self, interaction: discord.Interaction, minutes: int = 25, task: Optional[str] = "Deep Work"):
        minutes = max(5, min(minutes, 120))
        target_timestamp = time.time() + (minutes * 60)
        end_time_discord = int(target_timestamp)

        embed = discord.Embed(
            title="🎯 Focus Mode Started!",
            description=f"{interaction.user.mention} entered **{minutes} min** of uninterrupted focus!\n\n"
                        f"📌 **Task:** {task}\n"
                        f"⏳ **Ends:** <t:{end_time_discord}:R> (<t:{end_time_discord}:t>)\n\n"
                        f"Turn off notifications, minimize distractions, and lock in! 🚀",
            color=0x5865F2
        )
        embed.set_footer(text="Rewards awarded upon completion: XP + Coins + Deep Worker badge")
        view = FocusControlView(interaction.user.id, task, target_timestamp, minutes)
        await interaction.response.send_message(embed=embed, view=view)

        async def _run_timer():
            await asyncio.sleep(minutes * 60)
            if view.cancelled:
                return

            u = await get_user(interaction.user.id)
            earned_xp = int(minutes * 1.5)
            earned_coins = int(minutes * 1.0)

            # Apply 2x XP booster if active
            if u.get("boost_xp_until", 0) > time.time():
                earned_xp *= 2

            u["xp"] += earned_xp
            u["coins"] += earned_coins
            u["focus_minutes"] = u.get("focus_minutes", 0) + minutes
            bump_season(u, xp=earned_xp)
            new_badges = grant_badges(u)
            mark_dirty(interaction.user.id)
            track("focus_minutes")

            comp_embed = discord.Embed(
                title="🏆 Focus Session Completed!",
                description=f"🎉 Phenomenal job {interaction.user.mention}! You completed **{minutes} min** of deep work on *{task}*!\n\n"
                            f"🎁 **Rewards:** +{earned_xp} XP, +{earned_coins} 🪙 coins!",
                color=0x57F287
            )
            if new_badges:
                comp_embed.add_field(name="🏅 Badges Earned", value=", ".join(new_badges), inline=False)
            try:
                await interaction.channel.send(content=interaction.user.mention, embed=comp_embed)
            except Exception:
                pass

        asyncio.create_task(_run_timer())

    @app_commands.command(name="focus_rooms", description="List voice lounges giving 1.5x Focus bonus XP")
    async def focus_rooms(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Run this inside a server.", ephemeral=True)
            return
        found = []
        for vc in interaction.guild.voice_channels:
            name_lower = vc.name.lower()
            if any(k in name_lower for k in ("focus", "pomodoro", "study", "lounge", "deep work")):
                found.append(f"• 🎙️ {vc.mention} (`{len(vc.members)}` active)")

        desc = "\n".join(found) if found else "No active focus rooms found! Any voice channel with `focus`, `pomodoro`, `study`, or `lounge` in its name automatically grants the bonus!"
        embed = discord.Embed(
            title="🎧 Focus Voice Lounges (1.5x Bonus XP)",
            description=(
                "Studying or working in designated focus lounges earns **1.5x bonus XP** per minute!\n\n"
                f"{desc}\n\n"
                "💡 Combine with `/focus` pomodoro timer or a `2x XP Booster` from `/shop` for fast leveling!"
            ),
            color=0x57F287
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(GamificationCog(bot))
