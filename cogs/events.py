from typing import Union
import time
import discord
from discord.ext import commands

from config import today_str, XP_PER_MSG, MSG_COOLDOWN_S
from database import get_user, mark_dirty, track
from utils.helpers import calc_level, grant_badges, apply_level_roles, bump_season, blog

_last_msg_ts = {}  # {user_id_str: timestamp}
_voice_join = {}   # {user_id_str: timestamp}


class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        uid = str(message.author.id)
        now = time.time()
        last = _last_msg_ts.get(uid, 0)
        u = await get_user(uid)
        day = today_str()

        u["messages"] += 1
        u["daily"].setdefault(day, {"messages": 0, "voice": 0, "reactions": 0})["messages"] += 1
        bump_season(u, messages=1)

        gained_level = None
        if now - last >= MSG_COOLDOWN_S:
            _last_msg_ts[uid] = now
            old_lvl = calc_level(u["xp"])
            xp_gain = XP_PER_MSG * 2 if u.get("boost_xp_until", 0) > now else XP_PER_MSG
            u["xp"] += xp_gain
            u["coins"] += 2  # trickle coins for chat
            bump_season(u, xp=xp_gain)
            if calc_level(u["xp"]) > old_lvl:
                gained_level = calc_level(u["xp"])

        mark_dirty(uid)

        if gained_level:
            grant_badges(u)
            await apply_level_roles(message.author, gained_level)
            try:
                embed = discord.Embed(
                    title="🎉 Level Up!",
                    description=f"{message.author.mention} reached **Level {gained_level}**! 🚀",
                    color=0xFEE75C
                )
                await message.channel.send(embed=embed)
            except Exception:
                pass

        await self.bot.process_commands(message)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: Union[discord.User, discord.Member]):
        if user.bot:
            return
        u = await get_user(user.id)
        day = today_str()
        u["reactions_added"] += 1
        u["daily"].setdefault(day, {"messages": 0, "voice": 0, "reactions": 0})["reactions"] += 1
        u["xp"] += 2
        u["coins"] += 1
        bump_season(u, xp=2)
        mark_dirty(user.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return

        uid = str(member.id)
        now = time.time()
        was_in = before.channel is not None
        is_in = after.channel is not None

        # User connected to voice
        if not was_in and is_in:
            _voice_join[uid] = now

        # User switched channels or left voice
        elif was_in:
            start = _voice_join.pop(uid, None)
            if start is not None:
                delta = int(now - start)
                if delta > 0:
                    u = await get_user(uid)
                    day = today_str()
                    ch_name = getattr(before.channel, "name", "").lower()
                    is_focus_room = any(w in ch_name for w in ("focus", "study", "deep", "lounge", "pomodoro"))
                    mult = 1.5 if is_focus_room else 1.0
                    if u.get("boost_xp_until", 0) > now:
                        mult *= 2.0
                    xp_gain = int((delta // 60) * mult)
                    u["voice_seconds"] += delta
                    u["xp"] += xp_gain
                    u["coins"] += delta // 300  # 1 coin per 5 voice-min
                    bump_season(u, xp=xp_gain, voice=delta)
                    u["daily"].setdefault(day, {"messages": 0, "voice": 0, "reactions": 0})["voice"] += delta
                    if day not in u["voice_days"]:
                        u["voice_days"].append(day)
                    mark_dirty(uid)

            if is_in and before.channel != after.channel:
                _voice_join[uid] = now


async def setup(bot: commands.Bot):
    await bot.add_cog(EventsCog(bot))
