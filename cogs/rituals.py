"""Team rituals cog: standups, kudos, and bounty boards."""
from datetime import datetime, timezone
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands
from bson import ObjectId

from config import today_str
from database import (
    get_user, mark_dirty, track, standups_col, bounties_col
)
from utils.helpers import bump_season, grant_badges
from utils.views import BountyDecisionView


class RitualsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="standup", description="Post daily standup (yesterday / today / blockers)")
    @app_commands.describe(yesterday="What did you do yesterday?", today="What will you do today?", blockers="Any blockers?")
    async def standup(self, interaction: discord.Interaction, yesterday: str, today: str, blockers: Optional[str] = "None"):
        await interaction.response.defer()
        day = today_str()
        uid = str(interaction.user.id)
        doc = {
            "user_id": uid,
            "user_name": interaction.user.display_name,
            "date": day,
            "yesterday": yesterday,
            "today": today,
            "blockers": blockers,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await standups_col.replace_one({"user_id": uid, "date": day}, doc, upsert=True)
        track("standups")
        track("commands", "standup")

        embed = discord.Embed(
            title=f"🧍 Standup — {interaction.user.display_name} ({day})",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="⏪ Yesterday", value=yesterday[:1000], inline=False)
        embed.add_field(name="⏩ Today", value=today[:1000], inline=False)
        embed.add_field(name="🚧 Blockers", value=blockers[:1000] if blockers else "None", inline=False)
        embed.set_footer(text="Logged in database · Glyte Rituals")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="standup_list", description="Show compiled standups for a day (managers)")
    @app_commands.describe(date="YYYY-MM-DD (default: today)")
    async def standup_list(self, interaction: discord.Interaction, date: Optional[str] = None):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
            return
        await interaction.response.defer()
        day = date or today_str()
        docs = [d async for d in standups_col.find({"date": day})]
        if not docs:
            await interaction.followup.send(f"No standups posted for {day}.")
            return
        embed = discord.Embed(title=f"🧍 Standups for {day} ({len(docs)} members)", color=0x5865F2)
        for d in docs[:15]:
            val = f"**Y:** {d['yesterday'][:100]}\n**T:** {d['today'][:100]}\n**B:** {d.get('blockers','None')[:60]}"
            embed.add_field(name=f"👤 {d.get('user_name', d['user_id'])}", value=val, inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="kudos", description="Give a public shout-out + coin tip (3/day)")
    @app_commands.describe(member="Who to appreciate", reason="Why they rock")
    async def kudos(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ No self-kudos. 😏", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("❌ Bots don't need kudos. 🤖", ephemeral=True)
            return
        await interaction.response.defer()
        g = await get_user(interaction.user.id)
        day = today_str()
        kd = g.setdefault("kudos_day", {})
        if kd.get(day, 0) >= 3:
            await interaction.followup.send("⏳ Kudos limit reached (3/day).", ephemeral=True)
            return
        r = await get_user(member.id)
        r["kudos_received"] += 1
        r["coins"] += 20
        g["kudos_given"] += 1
        kd[day] = kd.get(day, 0) + 1
        g["xp"] += 5
        bump_season(g, xp=5)
        grant_badges(r)
        mark_dirty(interaction.user.id)
        mark_dirty(member.id)
        track("kudos")
        track("commands", "kudos")
        await interaction.followup.send(embed=discord.Embed(
            title="🙌 Kudos!",
            description=f"{interaction.user.mention} → {member.mention} (+20 🪙)\n> {reason}",
            color=0xFEE75C))

    @app_commands.command(name="bounty_post", description="Post a paid task (coins escrowed, managers)")
    @app_commands.describe(title="Task title", coins="Reward coins", description="Details")
    async def bounty_post(self, interaction: discord.Interaction, title: str, coins: int,
                          description: Optional[str] = None):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
            return
        if coins <= 0:
            await interaction.response.send_message("Coins must be positive.", ephemeral=True)
            return
        await interaction.response.defer()
        u = await get_user(interaction.user.id)
        if u["coins"] < coins:
            await interaction.followup.send(
                f"❌ You have {u['coins']} 🪙, need {coins}.", ephemeral=True)
            return
        u["coins"] -= coins
        mark_dirty(interaction.user.id)
        res = await bounties_col.insert_one({
            "title": title, "description": description or "—", "coins": coins,
            "poster_id": str(interaction.user.id), "status": "open", "claimer_id": None,
            "created_at": datetime.now(timezone.utc).isoformat()})
        await interaction.followup.send(
            f"🎯 Bounty posted! `{res.inserted_id}` **{title}** — {coins} 🪙 (escrowed)")

    @app_commands.command(name="bounty_list", description="Show open bounties")
    async def bounty_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = [d async for d in bounties_col.find(
            {"status": {"$in": ["open", "claimed"]}}).sort("created_at", -1).limit(10)]
        if not rows:
            await interaction.followup.send("No open bounties. 🎯", ephemeral=True)
            return
        lines = []
        for r in rows:
            extra = f" · claimed by <@{r['claimer_id']}>" if r["status"] == "claimed" else ""
            lines.append(f"`{r['_id']}` **{r['title']}** — {r['coins']} 🪙 · *{r['status']}*{extra}"
                         f"\n_{r['description']}_")
        await interaction.followup.send("\n\n".join(lines))

    @app_commands.command(name="bounty_claim", description="Claim an open bounty task")
    @app_commands.describe(bounty_id="Bounty id from /bounty_list")
    async def bounty_claim(self, interaction: discord.Interaction, bounty_id: str):
        await interaction.response.defer()
        try:
            oid = ObjectId(bounty_id)
        except Exception:
            await interaction.followup.send("❌ Bad bounty id.", ephemeral=True)
            return
        doc = await bounties_col.find_one({"_id": oid})
        if not doc or doc["status"] != "open":
            await interaction.followup.send("⚠️ That bounty is not open.", ephemeral=True)
            return
        if doc.get("poster_id") == str(interaction.user.id):
            await interaction.followup.send("❌ Can't claim your own bounty.", ephemeral=True)
            return
        await bounties_col.update_one(
            {"_id": oid, "status": "open"},
            {"$set": {"status": "claimed", "claimer_id": str(interaction.user.id)}})

        poster_id = str(doc.get("poster_id", ""))
        embed = discord.Embed(
            title="🤝 Bounty Claimed!",
            description=f"**Task**: **{doc['title']}**\n"
                        f"**Claimer**: {interaction.user.mention}\n"
                        f"**Reward**: {doc['coins']} 🪙 (+25 XP)\n"
                        f"**Poster**: <@{poster_id}>\n\n"
                        f"_{doc.get('description', '—')}_\n\n"
                        f"When work is done, the poster or a manager can click below to release payout!",
            color=0x5865F2
        )
        embed.set_footer(text=f"Bounty ID: {bounty_id}")
        view = BountyDecisionView(
            bounty_id=bounty_id,
            poster_id=poster_id,
            claimer_id=str(interaction.user.id),
            coins=doc['coins'],
            title=doc['title']
        )
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="bounty_approve", description="Approve work and pay claimer (managers)")
    @app_commands.describe(bounty_id="Bounty id")
    async def bounty_approve(self, interaction: discord.Interaction, bounty_id: str):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            oid = ObjectId(bounty_id)
        except Exception:
            await interaction.followup.send("Bad id.", ephemeral=True)
            return
        doc = await bounties_col.find_one({"_id": oid})
        if not doc or doc["status"] != "claimed":
            await interaction.followup.send("Not claimed or already paid.", ephemeral=True)
            return
        await bounties_col.update_one({"_id": oid}, {"$set": {"status": "done"}})
        u = await get_user(doc["claimer_id"])
        u["coins"] += doc["coins"]
        u["xp"] += 25
        bump_season(u, xp=25)
        grant_badges(u)
        mark_dirty(doc["claimer_id"])
        await interaction.followup.send(
            f"💰 Paid {doc['coins']} 🪙 to <@{doc['claimer_id']}> for **{doc['title']}**!")


async def setup(bot: commands.Bot):
    await bot.add_cog(RitualsCog(bot))
