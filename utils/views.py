"""Interactive Discord UI components: buttons, modals, dropdowns, and views."""
import time
from datetime import datetime, timezone
import discord
from bson import ObjectId

from config import SHOP_DEFAULT, today_str
from database import (
    get_config, get_user, mark_dirty, leaves_col, bounties_col, track
)
from utils.helpers import blog, bump_season, grant_badges


# ---- LEAVE FLOW VIEWS ----
class LeaveDecisionView(discord.ui.View):
    def __init__(self, leave_id: str, applicant_id: str, days: int):
        super().__init__(timeout=None)
        self.leave_id = leave_id
        self.applicant_id = applicant_id
        self.days = days

    @discord.ui.button(label="Approve ✅", style=discord.ButtonStyle.success)
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server permission required to approve leaves.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            oid = ObjectId(self.leave_id)
        except Exception:
            await interaction.followup.send("❌ Invalid leave ID.", ephemeral=True)
            return

        res = await leaves_col.update_one(
            {"_id": oid, "status": "pending"},
            {"$set": {"status": "approved", "decided_by": str(interaction.user.id), "decided_at": datetime.now(timezone.utc).isoformat()}}
        )
        if not res.modified_count:
            await interaction.followup.send("⚠️ Leave was already approved or rejected.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        orig_embed = interaction.message.embeds[0] if interaction.message.embeds else None
        new_embed = orig_embed.copy() if orig_embed else discord.Embed(title="🌴 Leave Request")
        new_embed.color = 0x57F287
        new_embed.add_field(name="Decision", value=f"✅ **Approved** by {interaction.user.mention}", inline=False)
        await interaction.message.edit(embed=new_embed, view=self)
        await interaction.followup.send(f"✅ Approved leave for <@{self.applicant_id}> ({self.days} days).", ephemeral=True)

        try:
            applicant = interaction.guild.get_member(int(self.applicant_id)) or await interaction.client.fetch_user(int(self.applicant_id))
            if applicant:
                await applicant.send(f"🌴 Good news! Your leave request for **{self.days} day(s)** was **Approved** by {interaction.user.display_name} in {interaction.guild.name}! Enjoy your time off! ✨")
        except Exception:
            pass

    @discord.ui.button(label="Reject ❌", style=discord.ButtonStyle.danger)
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server permission required to reject leaves.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            oid = ObjectId(self.leave_id)
        except Exception:
            await interaction.followup.send("❌ Invalid leave ID.", ephemeral=True)
            return

        res = await leaves_col.update_one(
            {"_id": oid, "status": "pending"},
            {"$set": {"status": "rejected", "decided_by": str(interaction.user.id), "decided_at": datetime.now(timezone.utc).isoformat()}}
        )
        if not res.modified_count:
            await interaction.followup.send("⚠️ Leave was already approved or rejected.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        orig_embed = interaction.message.embeds[0] if interaction.message.embeds else None
        new_embed = orig_embed.copy() if orig_embed else discord.Embed(title="🌴 Leave Request")
        new_embed.color = 0xED4245
        new_embed.add_field(name="Decision", value=f"❌ **Rejected** by {interaction.user.mention}", inline=False)
        await interaction.message.edit(embed=new_embed, view=self)
        await interaction.followup.send(f"❌ Rejected leave for <@{self.applicant_id}>.", ephemeral=True)

        try:
            applicant = interaction.guild.get_member(int(self.applicant_id)) or await interaction.client.fetch_user(int(self.applicant_id))
            if applicant:
                await applicant.send(f"⚠️ Your leave request for **{self.days} day(s)** in {interaction.guild.name} was **Rejected** by {interaction.user.display_name}.")
        except Exception:
            pass


class LeaveApplyModal(discord.ui.Modal, title="🌴 Submit Leave Application"):
    days_input = discord.ui.TextInput(
        label="Number of Days",
        placeholder="e.g. 1, 2, 5",
        default="1",
        min_length=1,
        max_length=2,
        required=True
    )
    from_date_input = discord.ui.TextInput(
        label="Start Date (YYYY-MM-DD)",
        placeholder="Leave empty for today",
        required=False,
        max_length=10
    )
    reason_input = discord.ui.TextInput(
        label="Reason for Leave",
        style=discord.TextStyle.paragraph,
        placeholder="Doctor's appointment, vacation, personal rest...",
        min_length=3,
        max_length=400,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            days = max(1, min(int(self.days_input.value.strip()), 30))
        except ValueError:
            await interaction.followup.send("❌ Days must be a valid number between 1 and 30.", ephemeral=True)
            return

        raw_from = self.from_date_input.value.strip()
        try:
            d0 = datetime.strptime(raw_from, "%Y-%m-%d").date() if raw_from else datetime.now(timezone.utc).date()
        except ValueError:
            await interaction.followup.send("❌ Date must be in format YYYY-MM-DD.", ephemeral=True)
            return

        reason = self.reason_input.value.strip()
        doc = {
            "user_id": str(interaction.user.id),
            "days": days,
            "reason": reason,
            "from": d0.isoformat(),
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
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


# ---- BOUNTY FLOW VIEWS ----
class BountyDecisionView(discord.ui.View):
    def __init__(self, bounty_id: str, poster_id: str, claimer_id: str, coins: int, title: str):
        super().__init__(timeout=None)
        self.bounty_id = bounty_id
        self.poster_id = poster_id
        self.claimer_id = claimer_id
        self.coins = coins
        self.title = title

    @discord.ui.button(label="Approve Work & Pay 💰", style=discord.ButtonStyle.success)
    async def approve_pay(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_poster = str(interaction.user.id) == self.poster_id
        is_manager = bool(interaction.user.guild_permissions.manage_guild)
        if not (is_poster or is_manager):
            await interaction.response.send_message("❌ Only the bounty poster or a manager can approve payout.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            oid = ObjectId(self.bounty_id)
        except Exception:
            await interaction.followup.send("❌ Bad bounty ID.", ephemeral=True)
            return
        res = await bounties_col.update_one({"_id": oid, "status": "claimed"}, {"$set": {"status": "done"}})
        if not res.modified_count:
            await interaction.followup.send("⚠️ Bounty was already paid or not claimed.", ephemeral=True)
            return

        u = await get_user(self.claimer_id)
        u["coins"] += self.coins
        u["xp"] += 25
        bump_season(u, xp=25)
        grant_badges(u)
        mark_dirty(self.claimer_id)

        for child in self.children:
            child.disabled = True

        orig_embed = interaction.message.embeds[0] if interaction.message.embeds else None
        new_embed = orig_embed.copy() if orig_embed else discord.Embed(title="🎯 Bounty Complete")
        new_embed.color = 0x57F287
        new_embed.add_field(name="Payout Status", value=f"💰 **{self.coins} 🪙** paid to <@{self.claimer_id}> (+25 XP) by {interaction.user.mention}!", inline=False)
        await interaction.message.edit(embed=new_embed, view=self)
        await interaction.followup.send(f"💰 Payout complete! Paid **{self.coins} 🪙** to <@{self.claimer_id}>.", ephemeral=True)


# ---- FOCUS MODE VIEWS ----
class FocusControlView(discord.ui.View):
    def __init__(self, user_id: int, task_name: str, target_time: float, minutes: int):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.task_name = task_name
        self.target_time = target_time
        self.minutes = minutes
        self.cancelled = False

    @discord.ui.button(label="Cancel Session ⏹️", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Only the session owner can cancel.", ephemeral=True)
            return
        self.cancelled = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="🛑 **Focus session cancelled early.** Take a breather!", view=self)


# ---- SHOP VIEWS ----
async def execute_purchase(interaction: discord.Interaction, item_id: str) -> tuple[bool, str]:
    cfg = await get_config()
    items = {it["id"]: it for it in cfg.get("shop", SHOP_DEFAULT)}
    item = items.get(item_id.lower().strip())
    if not item:
        return False, "❌ Unknown item. Check `/shop`."
    u = await get_user(interaction.user.id)
    if u["coins"] < item["cost"]:
        return False, f"❌ You need **{item['cost']} 🪙**, but you have **{u['coins']} 🪙**."

    u["coins"] -= item["cost"]
    u["inventory"].append({"id": item["id"], "name": item["name"], "at": today_str()})

    extra_msg = ""
    if item["id"] == "vip" and interaction.guild:
        vip_role = discord.utils.get(interaction.guild.roles, name="VIP Member 💎")
        if not vip_role:
            try:
                vip_role = await interaction.guild.create_role(
                    name="VIP Member 💎",
                    color=discord.Color.gold(),
                    hoist=True,
                    reason="Glyte Bot VIP Shop Item"
                )
            except Exception as e:
                blog(f"Failed to create VIP role: {e}")
        if vip_role and isinstance(interaction.user, discord.Member):
            try:
                await interaction.user.add_roles(vip_role, reason="Purchased VIP in /shop")
                extra_msg = "\n👑 **VIP Member 💎** role granted with golden styling!"
            except Exception as e:
                extra_msg = f"\n⚠️ Note: Bot could not assign role (check bot role permissions): {e}"

    mark_dirty(interaction.user.id)
    track("shop_buy", item["id"])
    track("commands", "buy")
    return True, f"🛍️ {interaction.user.mention} bought **{item['name']}** for **{item['cost']} 🪙**! Balance: **{u['coins']} 🪙**{extra_msg}"


class ShopSelect(discord.ui.Select):
    def __init__(self, items: list, user_id: int):
        self.user_id = user_id
        options = []
        for it in items[:25]:
            emoji = it.get("emoji")
            options.append(discord.SelectOption(
                label=f"{it['name']} ({it['cost']} 🪙)",
                value=it["id"],
                description=it.get("desc", "")[:100],
                emoji=emoji if emoji and len(emoji) <= 4 else None
            ))
        super().__init__(placeholder="🛒 Select an item to purchase...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This shop dropdown is for the person who ran `/shop`. Run your own `/shop`!", ephemeral=True)
            return
        item_id = self.values[0]
        success, msg = await execute_purchase(interaction, item_id)
        if success:
            await interaction.response.send_message(msg)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class ShopView(discord.ui.View):
    def __init__(self, items: list, user_id: int):
        super().__init__(timeout=180)
        self.add_item(ShopSelect(items, user_id))


# ---- HELP VIEWS ----
def get_help_embed(category: str = "overview") -> discord.Embed:
    if category == "attendance":
        embed = discord.Embed(
            title="🗓️ Attendance & Leave Commands",
            description="Keep track of your presence, build streaks, and manage time off 🌴",
            color=0x57F287
        )
        embed.add_field(name="✅ `/checkin`", value="Mark today's attendance! Earn **+20 XP**, **+25 🪙**, and grow your daily streak 🔥. Auto-protected by Streak Freeze 🧊.", inline=False)
        embed.add_field(name="🎁 `/daily`", value="Claim your free daily coin bonus (100–250 🪙 based on streak).", inline=False)
        embed.add_field(name="🗓️ `/attendance [@member] [days]`", value="View visual calendar presence timeline (`✅ check-in`, `🎙️ voice`, `🌴 leave`).", inline=False)
        embed.add_field(name="🌴 `/leave_apply [days] [reason]`", value="Apply for leave with an interactive popup modal and 1-click manager approval buttons.", inline=False)
        embed.add_field(name="📝 `/my_leaves`", value="Check status of your recent leave requests.", inline=False)

    elif category == "gamification":
        embed = discord.Embed(
            title="🎮 Gamification, Quests & Shop",
            description="Level up, complete daily challenges, spin the wheel, and buy server perks! 🏆",
            color=0xFEE75C
        )
        embed.add_field(name="📊 `/mystats [@member]`", value="Full profile: Level, XP progress bar, coins, streaks, badges, messages, and voice time.", inline=False)
        embed.add_field(name="🏆 `/leaderboard [category]`", value="Live leaderboards for XP, Coins, Messages, Voice time, and Check-ins.", inline=False)
        embed.add_field(name="🏁 `/season [month]`", value="Monthly competition standings + previous month champion 👑.", inline=False)
        embed.add_field(name="🗺️ `/quests` & `/quest_claim <id>`", value="View and claim daily quest rewards (+30–60 🪙 / XP).", inline=False)
        embed.add_field(name="🎰 `/spin`", value="Spin the Lucky Wheel daily! Win coins, XP, and jackpot badges 🎡.", inline=False)
        embed.add_field(name="🎯 `/focus [minutes] [task]`", value="Pomodoro deep work timer (5–120m) with XP & coin rewards.", inline=False)
        embed.add_field(name="🏪 `/shop`", value="Interactive rewards shop with 1-click dropdown menu! Buy boosters, VIP roles & perks.", inline=False)
        embed.add_field(name="⚡ `/use <item_id>`", value="Consume inventory items (`xp2x` 2x booster, `spinticket` lucky spin reset).", inline=False)
        embed.add_field(name="🎒 `/inventory` & 🏅 `/badges`", value="Showcase your earned items and prestigious badges.", inline=False)
        embed.add_field(name="🎧 `/focus_rooms`", value="List voice channels granting **1.5x bonus XP** while studying/working.", inline=False)

    elif category == "rituals":
        embed = discord.Embed(
            title="🤝 Team Rituals & Bounties",
            description="Collaborate, appreciate teammates, and get paid for bounties! 💸",
            color=0x5865F2
        )
        embed.add_field(name="🧍 `/standup`", value="Share daily updates (yesterday / today / blockers) in morning thread.", inline=False)
        embed.add_field(name="🙌 `/kudos @member <reason>`", value="Public peer appreciation + tip 20 🪙 to a teammate (3/day).", inline=False)
        embed.add_field(name="🎯 `/bounty_post <title> <coins> [desc]`", value="Post a paid task with coins held in escrow *(Managers)*.", inline=False)
        embed.add_field(name="📋 `/bounty_list`", value="Browse all active and claimed bounties.", inline=False)
        embed.add_field(name="🤝 `/bounty_claim <id>`", value="Claim a bounty task. Generates a 1-click payout button card.", inline=False)
        embed.add_field(name="💰 `/bounty_approve <id>`", value="Release escrowed coin payout to the worker.", inline=False)

    elif category == "social":
        embed = discord.Embed(
            title="🎂 Social & Reminders",
            description="Never miss a birthday or an important reminder! ✨",
            color=0xEB459E
        )
        embed.add_field(name="🎂 `/birthday set <month> <day>`", value="Save your birthday for server celebration cards and +150 🪙 / +100 XP gift!", inline=False)
        embed.add_field(name="🎉 `/birthday list`", value="List all upcoming server birthdays.", inline=False)
        embed.add_field(name="⏰ `/remindme <minutes> <message>`", value="Smart reminder timer that pings you in chat (or DMs if private).", inline=False)
        embed.add_field(name="💌 `/feedback <text>`", value="Send feedback directly to the self-improving engine 🧠.", inline=False)
        embed.add_field(name="💜 `/about`", value="About Glyte Bot and GlyteTech.", inline=False)

    elif category == "admin":
        embed = discord.Embed(
            title="🛡️ Moderation & Server Management",
            description="Tools for server admins and managers (Manage Server permission) 👑",
            color=0xED4245
        )
        embed.add_field(name="🛡️ `/strike add @member <reason>`", value="Issue a strike. **Reaching 3 strikes triggers automatic 1-hour timeout** ⏳.", inline=False)
        embed.add_field(name="📋 `/strike list` & `/strike clear`", value="View and manage member strike history.", inline=False)
        embed.add_field(name="⏱️ `/slowmode <seconds> [channel]`", value="Quickly adjust text channel slowmode delay.", inline=False)
        embed.add_field(name="🔄 `/sync`", value="Force instantaneous re-sync of all slash commands with Discord.", inline=False)
        embed.add_field(name="📊 `/dashboard [hours]`", value="Get your private auto-login admin control room magic link.", inline=False)
        embed.add_field(name="🧠 `/insights`", value="View team health metrics, quest auto-tunes, and suggestions.", inline=False)
        embed.add_field(name="🌙 `/eod`", value="Post today's End-Of-Day attendance summary immediately.", inline=False)
        embed.add_field(name="📈 `/report [days]`", value="Export attendance, chat, and voice metrics as a CSV file.", inline=False)
        embed.add_field(name="🏪 `/shop_add` & `/shop_remove`", value="Create or remove custom perks from the shop database.", inline=False)
        embed.add_field(name="🎖️ `/reward_add` & 🪙 `/give_coins`", value="Set up level-up roles or grant coins manually.", inline=False)

    else:
        embed = discord.Embed(
            title="🤖 Glyte Bot — Commands & Features Guide",
            description=(
                "Welcome to **Glyte Bot**! 🚀 Attendance, activity tracking, and full gamification for teams.\n\n"
                "✨ **Quickstart Essentials:**\n"
                "• ✅ `/checkin` — Check in daily to build your streak & earn rewards\n"
                "• 🗺️ `/quests` — Complete daily challenges for big coin/XP boosts\n"
                "• 🎰 `/spin` — Free daily spin on the Lucky Wheel\n"
                "• 🏪 `/shop` — Interactive rewards shop with boosters & perks\n"
                "• 📊 `/dashboard` — Your private live web dashboard link\n\n"
                "👇 **Select a category from the dropdown below to explore all commands!**"
            ),
            color=0x5865F2
        )
        embed.add_field(name="📂 Available Categories", value=(
            "• 🗓️ **Attendance & Leaves**\n"
            "• 🎮 **Gamification & Shop**\n"
            "• 🤝 **Team Rituals & Bounties**\n"
            "• 🎂 **Social & Reminders**\n"
            "• 🛡️ **Admin & Moderation**"
        ), inline=False)
        embed.add_field(name="📖 Full Manual", value="See `COMMANDS.md` in the repository for full parameter documentation.", inline=False)

    embed.set_footer(text="Glyte Bot • Built with 💜 by GlyteTech")
    return embed


class HelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="🌟 Quickstart & Overview", value="overview", description="Getting started, streaks, and core tips", emoji="🌟"),
            discord.SelectOption(label="🗓️ Attendance & Leaves", value="attendance", description="/checkin, /daily, /attendance, /leave_apply", emoji="🗓️"),
            discord.SelectOption(label="🎮 Gamification & Shop", value="gamification", description="/mystats, /quests, /shop, /buy, /use, /spin", emoji="🎮"),
            discord.SelectOption(label="🤝 Rituals & Bounties", value="rituals", description="/standup, /kudos, /bounty_post, /bounty_claim", emoji="🤝"),
            discord.SelectOption(label="🎂 Social & Reminders", value="social", description="/birthday, /remindme, /about, /feedback", emoji="🎂"),
            discord.SelectOption(label="🛡️ Admin & Moderation", value="admin", description="/strike, /slowmode, /sync, /dashboard, /insights", emoji="🛡️"),
        ]
        super().__init__(placeholder="📂 Choose a category to explore commands...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        cat = self.values[0]
        embed = get_help_embed(cat)
        await interaction.response.edit_message(embed=embed)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(HelpSelect())
