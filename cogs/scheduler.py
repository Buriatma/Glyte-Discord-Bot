"""Background tasks: periodic database cache flush and scheduled tasks (birthdays, standups, EOD, quest auto-tuning)."""
from datetime import datetime, timedelta
import discord
from discord.ext import tasks, commands

from config import (
    sched_tz, season_str, SUMMARY_CHANNEL_ID, SUMMARY_TIME,
    STANDUP_CHANNEL_ID, STANDUP_TIME, FLUSH_EVERY_S
)
from database import (
    flush_cache, get_config, save_config, get_user, mark_dirty,
    users_col, dash_access_col
)
from utils.helpers import blog
from utils.rituals import build_eod, tune_quests, award_season_champion


class SchedulerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.autosave_task.start()
        self.scheduler_task.start()

    def cog_unload(self):
        self.autosave_task.cancel()
        self.scheduler_task.cancel()

    @tasks.loop(seconds=FLUSH_EVERY_S)
    async def autosave_task(self):
        await flush_cache()

    @autosave_task.before_loop
    async def before_autosave(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def scheduler_task(self):
        try:
            now = datetime.now(sched_tz())
            hm = now.strftime("%H:%M")
            today = now.date().isoformat()
            cfg = await get_config()
            changed = False

            # Monthly season rotation
            cur = season_str()
            if cfg.get("current_season") != cur:
                prev = cfg.get("current_season")
                cfg["current_season"] = cur
                changed = True
                if prev:
                    await award_season_champion(prev, self.bot)

            # Daily quest auto-tuning
            if cfg.get("last_tune") != today:
                cfg["last_tune"] = today
                changed = True
                for a in await tune_quests(cfg):
                    blog(f"🧠 tuned quest {a['quest']}: {a['old']}→{a['new']} (rate {a['rate']})")

            # Daily birthday celebrations & rewards
            if cfg.get("last_birthday_check") != today:
                cfg["last_birthday_check"] = today
                changed = True
                mm_dd = now.strftime("%m-%d")
                async for doc in users_col.find({"birthday": mm_dd}):
                    uid = doc["_id"]
                    b_user = await get_user(uid)
                    b_user["coins"] += 150
                    b_user["xp"] += 100
                    mark_dirty(uid)

                    announce_ch = None
                    if SUMMARY_CHANNEL_ID:
                        announce_ch = self.bot.get_channel(SUMMARY_CHANNEL_ID)
                    if not announce_ch:
                        for g in self.bot.guilds:
                            if g.get_member(int(uid)):
                                announce_ch = g.system_channel or (g.text_channels[0] if g.text_channels else None)
                                break
                    if announce_ch:
                        try:
                            embed = discord.Embed(
                                title="🎂 Happy Birthday! 🎉",
                                description=f"Today is a special celebration! Happy Birthday to <@{uid}>! 🥳✨\n\n"
                                            f"🎁 Birthday gift: **+150 🪙 coins** and **+100 XP**!",
                                color=0xFEE75C
                            )
                            await announce_ch.send(content=f"🎉 <@{uid}>", embed=embed)
                        except Exception as e:
                            blog(f"Birthday announce failed: {e}")

            # Prune old dashboard access logs (30d)
            try:
                cutoff = (now - timedelta(days=30)).isoformat()
                await dash_access_col.delete_many({"at": {"$lt": cutoff}})
            except Exception:
                pass

            # Automated EOD summary
            if SUMMARY_CHANNEL_ID and hm == SUMMARY_TIME and cfg.get("last_eod") != today:
                ch = self.bot.get_channel(SUMMARY_CHANNEL_ID)
                if ch and getattr(ch, "guild", None):
                    try:
                        await ch.send(embed=await build_eod(ch.guild, today))
                        cfg["last_eod"] = today
                        changed = True
                    except Exception as e:
                        blog(f"EOD post failed: {e}")

            # Automated morning standup prompt & thread
            if STANDUP_CHANNEL_ID and hm == STANDUP_TIME and cfg.get("last_standup") != today:
                ch = self.bot.get_channel(STANDUP_CHANNEL_ID)
                if ch:
                    try:
                        msg = await ch.send(
                            f"🧍 **Daily standup — {today}**\n"
                            f"Drop your update with `/standup` (yesterday / today / blockers) 👇"
                        )
                        try:
                            await msg.create_thread(name=f"standup-{today}", auto_archive_duration=1440)
                        except Exception:
                            pass
                        cfg["last_standup"] = today
                        changed = True
                    except Exception as e:
                        blog(f"Standup prompt failed: {e}")

            if changed:
                await save_config(cfg)
        except Exception as e:
            blog(f"Scheduler loop error: {e}")

    @scheduler_task.before_loop
    async def before_scheduler(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulerCog(bot))
