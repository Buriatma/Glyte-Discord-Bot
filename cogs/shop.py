"""Rewards shop and inventory management cog."""
import time
import discord
from discord import app_commands
from discord.ext import commands

from config import SHOP_DEFAULT
from database import (
    get_config, save_config, get_user, mark_dirty
)
from utils.views import (
    ShopView, execute_purchase
)


class ShopCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="shop", description="Browse and purchase rewards, perks & boosters")
    async def shop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        cfg = await get_config()
        items = cfg.get("shop", SHOP_DEFAULT)
        u = await get_user(interaction.user.id)

        embed = discord.Embed(
            title="🏪 Glyte Rewards & Perks Shop",
            description=f"Welcome, {interaction.user.mention}! Your balance: **{u['coins']} 🪙**\n"
                        f"Choose an item from the dropdown below or run `/buy <item_id>`.\n",
            color=0xEB459E
        )
        for it in items:
            embed.add_field(
                name=f"{it.get('emoji', '🎁')} {it['name']} — `{it['cost']} 🪙`",
                value=f"{it['desc']}\n*ID:* `{it['id']}`",
                inline=False
            )
        embed.set_footer(text="💡 Tip: Items like 2x XP Booster & Spin Tickets can be used with /use <item_id>!")
        view = ShopView(items, interaction.user.id)
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="buy", description="Buy an item from the shop with coins")
    @app_commands.describe(item_id="Item id from /shop (e.g. freeze, xp2x, spinticket, vip)")
    async def buy(self, interaction: discord.Interaction, item_id: str):
        await interaction.response.defer()
        success, msg = await execute_purchase(interaction, item_id)
        if success:
            await interaction.followup.send(msg)
        else:
            await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="use", description="Use/activate an item from your inventory (e.g. xp2x, spinticket)")
    @app_commands.describe(item_id="ID of the item in your inventory to consume")
    async def use_item(self, interaction: discord.Interaction, item_id: str):
        await interaction.response.defer()
        item_id = item_id.lower().strip()
        u = await get_user(interaction.user.id)
        inv = u.get("inventory", [])

        idx = next((i for i, it in enumerate(inv) if it.get("id") == item_id), None)
        if idx is None:
            await interaction.followup.send(
                f"❌ You don't have `{item_id}` in your inventory. Check `/inventory` or visit `/shop`!",
                ephemeral=True
            )
            return

        if item_id == "freeze":
            await interaction.followup.send(
                "🧊 **Streak Freeze is passive!** Keep it safely in your inventory — if you miss a daily `/checkin`, it will automatically consume itself to save your streak! 🔥",
                ephemeral=True
            )
            return

        used = inv.pop(idx)
        now_ts = time.time()

        if item_id == "xp2x":
            current_boost = max(now_ts, u.get("boost_xp_until", 0))
            u["boost_xp_until"] = current_boost + 7200  # 2 hours
            mark_dirty(interaction.user.id)
            rem_mins = int((u["boost_xp_until"] - now_ts) // 60)
            await interaction.followup.send(
                f"⚡ {interaction.user.mention} activated **2x XP Booster**! Double XP active on messages & voice for the next **{rem_mins} minutes**!"
            )
        elif item_id == "spinticket":
            u["last_spin"] = None
            mark_dirty(interaction.user.id)
            await interaction.followup.send(
                f"🎟️ {interaction.user.mention} used a **Lucky Spin Ticket**! Your spin cooldown is reset. Run `/spin` now! 🎰"
            )
        elif item_id in ("coffee", "earlylog", "wfh", "mvp"):
            mark_dirty(interaction.user.id)
            ticket_no = hex(int(now_ts))[2:].upper()
            await interaction.followup.send(
                f"🎉 {interaction.user.mention} redeemed **{used['name']}** (Ticket: `#{ticket_no}`)!\n"
                f"Please notify your manager for fulfillment."
            )
        else:
            mark_dirty(interaction.user.id)
            await interaction.followup.send(
                f"✨ {interaction.user.mention} used **{used['name']}**!"
            )

    @app_commands.command(name="inventory", description="Show your owned items")
    async def inventory(self, interaction: discord.Interaction):
        await interaction.response.defer()
        u = await get_user(interaction.user.id)
        inv = u.get("inventory", [])
        desc = "\n".join(f"• **{it['name']}** (`{it['id']}`, {it.get('at', '?')})" for it in inv[-20:]) or "Empty. Visit /shop!"
        await interaction.followup.send(embed=discord.Embed(
            title=f"🎒 {interaction.user.display_name}'s Inventory", description=desc, color=0xEB459E))

    @app_commands.command(name="shop_add", description="[Admin] Add or update a shop item")
    @app_commands.describe(
        item_id="Unique identifier (e.g. pizza)",
        name="Item name (e.g. 🍕 Free Pizza)",
        cost="Cost in coins",
        desc="Item description"
    )
    async def shop_add(self, interaction: discord.Interaction, item_id: str, name: str, cost: int, desc: str):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cfg = await get_config()
        items = cfg.get("shop", list(SHOP_DEFAULT))
        item_id = item_id.lower().strip()
        idx = next((i for i, it in enumerate(items) if it["id"] == item_id), None)
        entry = {"id": item_id, "name": name, "cost": max(1, cost), "desc": desc, "emoji": name.split()[0] if name else "🎁"}
        if idx is not None:
            items[idx] = entry
        else:
            items.append(entry)
        cfg["shop"] = items
        await save_config(cfg)
        await interaction.followup.send(f"✅ Item `{item_id}` ({name}) added/updated in `/shop` for {cost} 🪙!", ephemeral=True)

    @app_commands.command(name="shop_remove", description="[Admin] Remove an item from the shop")
    @app_commands.describe(item_id="ID of the item to remove")
    async def shop_remove(self, interaction: discord.Interaction, item_id: str):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Manage Server required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cfg = await get_config()
        items = cfg.get("shop", list(SHOP_DEFAULT))
        item_id = item_id.lower().strip()
        new_items = [it for it in items if it["id"] != item_id]
        if len(new_items) == len(items):
            await interaction.followup.send(f"❌ Item `{item_id}` not found in shop.", ephemeral=True)
            return
        cfg["shop"] = new_items
        await save_config(cfg)
        await interaction.followup.send(f"✅ Item `{item_id}` removed from `/shop`!", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ShopCog(bot))
