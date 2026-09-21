"""Attendance and leave management cog."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands
from bson import ObjectId

from config import (
    STREAK_BONUS, today_str, yesterday_str
)
from database import (
    get_user, mark_dirty, flush_cache, track, leaves_col
)
from utils.helpers import (
    calc_level, xp_into_level, progress_bar, fmt_duration,
    grant_badges, bump_season, apply_level_roles
)
from utils.views import LeaveDecisionView, LeaveApplyModal


class AttendanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="checkin", description="Mark today's attendance, streak + rewards")
    async def checkin(self, interaction: discord.Interaction):
        await interaction.response.defer()
        u = await get_user(interaction.user.id)
        today = today_str()
        if today in u["checkins"]:
            await interaction.followup.send(
                f"✅ Already checked in today! Streak: **{u['streak']}** 🔥", ephemeral=True)
            return

        used_freeze = False
        if u.get("last_checkin") == yesterday_str():
            u["streak"] = u.get("streak", 0) + 1
        else:
            inv = u.get("inventory", [])
            freeze_idx = next((i for i, it in enumerate(inv) if it.get("id") == "freeze"), None)
            if freeze_idx is not None and u.get("streak", 0) >= 2:
                inv.pop(freeze_idx)
                used_freeze = True
                u["streak"] = u.get("streak", 0) + 1
            else:
                u["streak"] = 1

        u["last_checkin"] = today
        u["checkins"].append(today)

        old_lvl = calc_level(u["xp"])
        u["xp"] += 20
        u["coins"] += 25
        bump_season(u, xp=20)
        bonus = STREAK_BONUS.get(u["streak"], 0)
        if bonus:
            u["coins"] += bonus
            u["badges"] = sorted(set(u["badges"]) | {f"streak-{u['streak']}"})
        new_badges = grant_badges(u)
        mark_dirty(interaction.user.id)
        track("checkins")
        track("commands", "checkin")
        asyncio.create_task(flush_cache())

        new_lvl = calc_level(u["xp"])
        if new_lvl > old_lvl:
            await apply_level_roles(interaction.user, new_lvl)

        msg = (f"✅ {interaction.user.mention} checked in for **{today}**! 🔥 Streak **{u['streak']}** "
               f"(+20 XP, +25 coins)")
        if used_freeze:
            msg += " 🧊 **Streak Freeze Activated!** Your streak was protected from resetting! 🔥"
        if bonus:
            msg += f" 🎁 Streak bonus +{bonus} coins!"
        if new_lvl > old_lvl:
            msg += f" 🎉 Level up → **{new_lvl}**!"
        if new_badges:
            msg += f" 🏅 New badges: {', '.join(new_badges)}"
        await interaction.followup.send(msg)

    @app_commands.command(name="daily", description="Claim daily coins (every 24h window by date)")
    async def daily(self, interaction: discord.Interaction):
        await interaction.response.defer()
        u = await get_user(interaction.user.id)
        today = today_str()
        if u.get("last_daily") == today:
            await interaction.followup.send("⏳ Already claimed today. Come back tomorrow!", ephemeral=True)
            return
        u["last_daily"] = today
        reward = 100 + min(u.get("streak", 0), 30) * 5
        u["coins"] += reward
        mark_dirty(interaction.user.id)
        await interaction.followup.send(f"🎁 +{reward} coins! Balance: **{u['coins']}** 🪙")

    @app_commands.command(name="mystats", description="Show your (or another member's) gamified stats")
    @app_commands.describe(member="Member to look up (default: you)")
    async def mystats(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer()
        member = member or interaction.user
        u = await get_user(member.id)
        lvl, into, need = xp_into_level(u["xp"])
        embed = discord.Embed(title=f"📊 {member.display_name} — Lv {lvl}",
                              color=0x5865F2,
                              description=f"{progress_bar(into, need)} `{into}/{need} XP`")
        embed.add_field(name="🪙 Coins", value=str(u["coins"]), inline=True)
        embed.add_field(name="✨ XP", value=str(u["xp"]), inline=True)
        embed.add_field(name="💬 Messages", value=str(u["messages"]), inline=True)
        embed.add_field(name="🎙️ Voice", value=fmt_duration(u["voice_seconds"]), inline=True)
        embed.add_field(name="⭐ Reactions", value=str(u["reactions_added"]), inline=True)
        embed.add_field(name="✅ Check-ins", value=str(len(u["checkins"])), inline=True)
        embed.add_field(name="🔥 Streak", value=str(u["streak"]), inline=True)
        embed.add_field(name="🙌 Kudos", value=str(u.get("kudos_received", 0)), inline=True)
        embed.add_field(name="🏅 Badges", value=", ".join(u.get("badges", [])[:10]) or "—", inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="attendance", description="Show attendance for last N days")
    @app_commands.describe(member="Member (default: you)", days="Last N days (max 30)")
    async def attendance(self, interaction: discord.Interaction,
                         member: Optional[discord.Member] = None, days: int = 7):
        await interaction.response.defer()
        member = member or interaction.user
        days = max(1, min(days, 30))
        u = await get_user(member.id)
        today = datetime.now(timezone.utc).date()
        checkins = set(u.get("checkins", []))
        voice_days = set(u.get("voice_days", []))
        leaves = [d async for d in leaves_col.find(
            {"user_id": str(member.id), "status": "approved"})]
        leave_days = set()
        for lv in leaves:
            d0 = datetime.fromisoformat(lv["from"]).date()
            for i in range(int(lv.get("days", 1))):
                leave_days.add((d0 + timedelta(days=i)).isoformat())
        lines, present = [], 0
        for i in range(days):
            d = (today - timedelta(days=i)).isoformat()
            if d in checkins or d in voice_days:
                present += 1
            c = "✅" if d in checkins else ("🌴" if d in leave_days else "⬜")
            v = "🎙️" if d in voice_days else ""
            lines.append(f"`{d}` {c}{v}")
        pct = round(100 * present / days)
        embed = discord.Embed(
            title=f"🗓️ Attendance — {member.display_name} ({present}/{days}d · {pct}%)",
            description="\n".join(reversed(lines)), color=0x5865F2)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="leave_apply", description="Apply for leave (interactive modal form or direct args)")
    @app_commands.describe(days="Number of days (opens popup form if omitted)", reason="Reason", from_date="Start YYYY-MM-DD")
    async def leave_apply(self, interaction: discord.Interaction,
                          days: Optional[int] = None,
                          reason: Optional[str] = None,
                          from_date: Optional[str] = None):
        if days is None or reason is None:
            await interaction.response.send_modal(LeaveApplyModal())
            return

        await interaction.response.defer()
        days = max(1, min(days, 30))
        try:
            d0 = datetime.strptime(from_date, "%Y-%m-%d").date() if from_date else datetime.now(timezone.utc).date()
        except ValueError:
            await interaction.followup.send("❌ from_date must be YYYY-MM-DD.", ephemeral=True)
            return

        doc = {"user_id": str(interaction.user.id), "days": days, "reason": reason,
               "from": d0.isoformat(), "status": "pending",
               "created_at": datetime.now(timezone.utc).isoformat()}
        res = await leaves_col.insert_one(doc)
        leave_id = str(res.inserted_id)

        embed = discord.Embed(
            title="📝 Leave Application Submitted",
            description=f"**Applicant**: {interaction.user.mention}\n"
                        f"**Duration**: {days} day(s)\n"
                        f"**Start Date**: `{d0.isoformat()}`\n"
                        f"**Reason**: {reason}",
            color=0xFEE75C
        )
        embed.set_footer(text=f"Leave ID: {leave_id} · Managers can approve or reject below")
        view = LeaveDecisionView(leave_id, str(interaction.user.id), days)
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="my_leaves", description="Show your leave requests")
    async def my_leaves(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = [d async for d in leaves_col.find(
            {"user_id": str(interaction.user.id)}).sort("created_at", -1).limit(10)]
        if not rows:
            await interaction.followup.send("No leaves yet.", ephemeral=True)
            return
        lines = [f"`{r['_id']}` {r['from']} ×{r['days']}d — **{r['status']}** — {r['reason']}" for r in rows]
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="leave_list", description="List pending leaves (managers)")
    async def leave_list(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        rows = [d async for d in leaves_col.find({"status": "pending"}).sort("created_at", -1).limit(15)]
        if not rows:
            await interaction.followup.send("No pending leaves. 🎉", ephemeral=True)
            return
        lines = [f"`{r['_id']}` <@{r['user_id']}> {r['from']} ×{r['days']}d — {r['reason']}" for r in rows]
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="leave_decide", description="Approve/reject a leave (managers)")
    @app_commands.describe(leave_id="Leave id from /leave_list", approve="Approve?")
    async def leave_decide(self, interaction: discord.Interaction, leave_id: str, approve: bool):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            oid = ObjectId(leave_id)
        except Exception:
            await interaction.followup.send("Bad leave id.", ephemeral=True)
            return
        res = await leaves_col.update_one(
            {"_id": oid, "status": "pending"},
            {"$set": {"status": "approved" if approve else "rejected",
                      "decided_by": str(interaction.user.id)}})
        if not res.modified_count:
            await interaction.followup.send("Not found or already decided.", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Leave {leave_id} {'approved' if approve else 'rejected'}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AttendanceCog(bot))
