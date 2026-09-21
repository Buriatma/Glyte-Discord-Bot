"""Moderation, administrative configuration, reports, sync, and dashboard controls."""
import csv
import json
import secrets
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands

from config import today_str, DASHBOARD_PUBLIC_URL
from database import (
    get_user, mark_dirty, track, flush_cache, get_config, save_config,
    users_col, strikes_col, dash_tokens_col, leaves_col
)
from utils.helpers import fmt_duration, blog
from utils.rituals import build_eod, build_insights


class ModerationCog(commands.Cog):
    strike_group = app_commands.Group(name="strike", description="Moderation strikes & warnings 🛡️")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @strike_group.command(name="add", description="[Mod] Issue a strike to a member (3 strikes = 1h auto-timeout)")
    @app_commands.describe(member="Member to strike", reason="Reason for the strike")
    async def strike_add(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ Manage Messages permission required.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("❌ Cannot strike a bot.", ephemeral=True)
            return
        if member.top_role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Cannot strike a member with equal or higher role.", ephemeral=True)
            return

        await interaction.response.defer()
        now_iso = datetime.now(timezone.utc).isoformat()
        await strikes_col.insert_one({
            "user_id": str(member.id),
            "guild_id": str(interaction.guild.id),
            "reason": reason,
            "mod_id": str(interaction.user.id),
            "at": now_iso
        })
        track("moderation", "strike_add")

        total = await strikes_col.count_documents({"user_id": str(member.id), "guild_id": str(interaction.guild.id)})
        extra = ""
        if total >= 3:
            try:
                await member.timeout(timedelta(hours=1), reason=f"Accumulated {total} strikes. Latest: {reason}")
                extra = "\n⚠️ **Threshold Reached!** Applied an automatic **1-hour timeout** ⏳"
            except Exception as e:
                extra = f"\n⚠️ Auto-timeout could not be applied: {e}"

        embed = discord.Embed(
            title=f"🛡️ Strike #{total} Issued",
            description=f"**User:** {member.mention}\n"
                        f"**Reason:** {reason}\n"
                        f"**Moderator:** {interaction.user.mention}\n"
                        f"**Total Strikes:** `{total}`{extra}",
            color=0xED4245
        )
        await interaction.followup.send(embed=embed)

    @strike_group.command(name="list", description="[Mod] View strikes for a member")
    @app_commands.describe(member="Member to view strikes for")
    async def strike_list(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ Manage Messages permission required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        strikes = [s async for s in strikes_col.find({"user_id": str(member.id), "guild_id": str(interaction.guild.id)}).sort("at", -1)]
        if not strikes:
            await interaction.followup.send(f"✅ {member.mention} has a clean record (0 strikes).", ephemeral=True)
            return
        lines = [f"• `{s['at'][:10]}` — **{s['reason']}** (by <@{s['mod_id']}>)" for s in strikes[:20]]
        embed = discord.Embed(
            title=f"🛡️ Strikes for {member.display_name} ({len(strikes)} total)",
            description="\n".join(lines),
            color=0xED4245
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @strike_group.command(name="clear", description="[Mod] Clear all strikes for a member")
    @app_commands.describe(member="Member to clear strikes for")
    async def strike_clear(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ Manage Messages permission required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        res = await strikes_col.delete_many({"user_id": str(member.id), "guild_id": str(interaction.guild.id)})
        await interaction.followup.send(f"✅ Cleared **{res.deleted_count}** strikes for {member.mention}!", ephemeral=True)


    @app_commands.command(name="sync", description="[Admin] Refresh and sync all slash commands immediately (fixes outdated cache)")
    async def sync_cmd(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server permission required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            # Sync to current guild for immediate 0ms update
            if interaction.guild:
                self.bot.tree.copy_global_to(guild=interaction.guild)
                synced_guild = await self.bot.tree.sync(guild=interaction.guild)
                guild_count = len(synced_guild)
            else:
                guild_count = 0

            # Sync globally
            synced_global = await self.bot.tree.sync()
            global_count = len(synced_global)

            track("commands", "sync")
            embed = discord.Embed(
                title="⚡ Slash Commands Refreshed!",
                description=(
                    f"Successfully synchronized:\n"
                    f"• **Guild Commands:** `{guild_count}` commands synced instantly to **{interaction.guild.name if interaction.guild else 'Server'}**\n"
                    f"• **Global Commands:** `{global_count}` commands pushed to Discord API\n\n"
                    f"💡 **Fixing 'This command is outdated':**\n"
                    f"If you or your members still see outdated commands or parameters, press **`Ctrl + R`** (Windows) or **`Cmd + R`** (Mac) / restart your Discord mobile app to reload Discord's local client cache."
                ),
                color=0x57F287
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Command synchronization failed: {e}", ephemeral=True)

    @app_commands.command(name="slowmode", description="[Mod] Set text channel slowmode delay")
    @app_commands.describe(seconds="Slowmode in seconds (0 to turn off, max 21600)", channel="Channel (default: current)")
    async def slowmode(self, interaction: discord.Interaction, seconds: int, channel: Optional[discord.TextChannel] = None):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ Manage Channels permission required.", ephemeral=True)
            return
        ch = channel or interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ Slowmode can only be applied to text channels.", ephemeral=True)
            return
        seconds = max(0, min(seconds, 21600))
        try:
            await ch.edit(slowmode_delay=seconds)
            status = f"set to **{seconds}s**" if seconds > 0 else "disabled"
            await interaction.response.send_message(f"⏱️ Slowmode {status} in {ch.mention}!")
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to set slowmode: {e}", ephemeral=True)

    @app_commands.command(name="reward_add", description="Map a level to a role (admin)")
    @app_commands.describe(level="Level number", role="Role to grant")
    async def reward_add(self, interaction: discord.Interaction, level: int, role: discord.Role):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cfg = await get_config()
        cfg.setdefault("rewards", {})[str(level)] = role.id
        await save_config(cfg)
        await interaction.followup.send(f"✅ Level {level} → {role.mention}", ephemeral=True)

    @app_commands.command(name="reward_list", description="Show level-role rewards")
    async def reward_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        cfg = await get_config()
        rewards = cfg.get("rewards", {})
        if not rewards:
            await interaction.followup.send("No level rewards configured yet. Use `/reward_add` to add role rewards.", ephemeral=True)
            return
        lines = [f"Lv **{lvl}** → <@&{rid}>" for lvl, rid in sorted(rewards.items(), key=lambda x: int(x[0]))]
        embed = discord.Embed(
            title="🎖️ Level Reward Roles",
            description="\n".join(lines),
            color=0xFEE75C
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="give_coins", description="Give coins (admin)")
    @app_commands.describe(member="Who to credit", amount="Amount of coins")
    async def give_coins(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
            return
        await interaction.response.defer()
        u = await get_user(member.id)
        u["coins"] += amount
        mark_dirty(member.id)
        track("economy", "give_coins")
        await interaction.followup.send(f"🪙 Added **{amount} coins** to {member.mention}. Balance: **{u['coins']}**")

    @app_commands.command(name="export", description="Export user database as JSON file (admin)")
    async def export(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await flush_cache()
        docs = [d async for d in users_col.find({})]
        for d in docs:
            d["user_id"] = str(d.pop("_id"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(docs, f, indent=2)
            path = f.name
        await interaction.followup.send("💾 Database Export:", file=discord.File(path, filename="glyte_users_export.json"))

    @app_commands.command(name="report", description="Weekly activity report as CSV (managers)")
    @app_commands.describe(days="Last N days (default 7, max 31)")
    async def report(self, interaction: discord.Interaction, days: int = 7):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        days = max(1, min(days, 31))
        await flush_cache()
        end = datetime.now(timezone.utc).date()
        dates = [(end - timedelta(days=i)).isoformat() for i in range(days)]
        date_set = set(dates)

        # Build approved leave map
        on_leave = {}
        async for lv in leaves_col.find({"status": "approved"}):
            uid = lv.get("user_id")
            s = lv.get("start_date")
            e = lv.get("end_date")
            if uid and s and e:
                try:
                    cur = datetime.strptime(s, "%Y-%m-%d").date()
                    stop = datetime.strptime(e, "%Y-%m-%d").date()
                    cur_set = on_leave.setdefault(uid, set())
                    while cur <= stop:
                        cur_set.add(cur.isoformat())
                        cur += timedelta(days=1)
                except Exception:
                    pass

        docs = [d async for d in users_col.find({})]
        names = {}
        if interaction.guild:
            for m in interaction.guild.members:
                names[str(m.id)] = m.display_name

        rows, top_msg, top_voice, total_check = [], ("—", 0), ("—", 0), 0
        for d in docs:
            uid = str(d["_id"])
            msgs = sum(d.get("daily", {}).get(x, {}).get("messages", 0) for x in dates)
            voice = sum(d.get("daily", {}).get(x, {}).get("voice", 0) for x in dates)
            checks = len(set(d.get("checkins", [])) & date_set)
            leaves = len(date_set & on_leave.get(uid, set()))
            total_check += checks
            nm = names.get(uid, uid)
            if msgs > top_msg[1]:
                top_msg = (nm, msgs)
            if voice > top_voice[1]:
                top_voice = (nm, voice)
            rows.append([uid, nm, msgs, round(voice / 60), d.get("xp", 0),
                         d.get("coins", 0), d.get("streak", 0), checks, leaves])
        rows.sort(key=lambda r: r[2], reverse=True)
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            w = csv.writer(f)
            w.writerow(["user_id", "name", "messages", "voice_min", "xp_total",
                        "coins", "streak", f"checkins_{days}d", f"leaves_{days}d"])
            w.writerows(rows)
            path = f.name

        embed = discord.Embed(title=f"📈 Report — last {days}d ({dates[-1]} → {dates[0]})", color=0x5865F2)
        embed.add_field(name="👥 Tracked", value=str(len(rows)), inline=True)
        embed.add_field(name="✅ Avg check-ins/day", value=str(round(total_check / days, 1)), inline=True)
        embed.add_field(name="💬 Top chatter", value=f"{top_msg[0]} ({top_msg[1]})", inline=True)
        embed.add_field(name="🎙️ Top voice", value=f"{top_voice[0]} ({fmt_duration(top_voice[1])})", inline=True)
        await interaction.followup.send(embed=embed, file=discord.File(path, filename=f"report-{days}d.csv"))

    @app_commands.command(name="eod", description="Post today's EOD summary now (managers)")
    async def eod(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        await interaction.response.defer()
        await interaction.followup.send(embed=await build_eod(interaction.guild, today_str()))

    @app_commands.command(name="insights", description="How the team is doing + bot self-tuning (managers)")
    async def insights(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
            return
        await interaction.response.defer()
        ins = await build_insights()
        embed = discord.Embed(title="🧠 Team Insights (self-improving bot)", color=0xEB459E)
        embed.add_field(name="👥 Members", value=str(ins["users"]), inline=True)
        embed.add_field(name="✅ Check-ins/7d", value=str(ins["checkins_7"]), inline=True)
        embed.add_field(name="🙌 Kudos/7d", value=str(ins["kudos_7"]), inline=True)
        qlines = "\n".join(f"{v['name']}: {int(v['rate']*100)}% (target {v['target']})"
                           for v in ins["quests"].values())
        embed.add_field(name="🗺️ Quest completion", value=qlines or "—", inline=False)
        embed.add_field(name="💡 Suggestions", value="\n".join(ins["suggestions"])[:1000], inline=False)
        if ins["tune_log"]:
            embed.add_field(name="🎯 Auto-tunes", value="\n".join(
                f"{a['at'][:10]} {a['quest']}: {a['old']}→{a['new']}" for a in ins["tune_log"]), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="dashboard", description="Your private dashboard magic link (only you see it)")
    @app_commands.describe(hours="Link validity in hours (default 24, max 72)")
    async def dashboard_cmd(self, interaction: discord.Interaction, hours: int = 24):
        await interaction.response.defer(ephemeral=True)
        hours = max(1, min(hours, 72))
        uid = str(interaction.user.id)
        is_admin = bool(interaction.user.guild_permissions.manage_guild) if interaction.guild else False
        await dash_tokens_col.delete_many({"user_id": uid})
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        await dash_tokens_col.insert_one({
            "token": token, "user_id": uid, "created_by": uid,
            "user_name": interaction.user.display_name,
            "is_admin": is_admin,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=hours)).isoformat()
        })
        track("commands", "dashboard")
        blog(f"📊 dashboard link for {interaction.user.display_name} (admin={is_admin})")
        role = "👑 Admin control room" if is_admin else "🙋 Your personal hub"
        link = f"{DASHBOARD_PUBLIC_URL.rstrip('/')}/dash/?token={token}"
        await interaction.followup.send(
            f"📊 **Your magic dashboard link** ✨ — {role}, auto-logged-in as you\n{link}\n"
            f"⏳ Valid **{hours}h** · only this link knows it's you 🔒",
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
