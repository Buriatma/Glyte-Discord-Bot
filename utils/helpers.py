"""Helper utility functions for math, formatting, logging, and badges."""
import logging
from collections import deque
from datetime import datetime, timezone
import discord
from config import season_str
from database import get_config

# Live ring buffer for the admin dashboard
_bot_logs: deque = deque(maxlen=300)
_glyte_log = logging.getLogger("glyte")


class _RingHandler(logging.Handler):
    def emit(self, record):
        try:
            _bot_logs.append(
                f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} "
                f"{record.levelname} {record.getMessage()}")
        except Exception:
            pass


_glyte_log.addHandler(_RingHandler())
_glyte_log.setLevel(logging.INFO)


def blog(msg: str):
    print(msg)
    _glyte_log.info(msg)


def calc_level(xp: int) -> int:
    return xp // 150 + 1


def xp_into_level(xp: int):
    lvl = calc_level(xp)
    base = (lvl - 1) * 150
    return lvl, xp - base, 150


def progress_bar(done: int, total: int, width: int = 10) -> str:
    frac = max(0.0, min(1.0, done / total)) if total else 0
    filled = int(round(frac * width))
    return "▰" * filled + "▱" * (width - filled)


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def bump_season(u: dict, xp: int = 0, messages: int = 0, voice: int = 0):
    row = u.setdefault("seasons", {}).setdefault(season_str(), {"xp": 0, "messages": 0, "voice": 0})
    row["xp"] += xp
    row["messages"] += messages
    row["voice"] += voice


def grant_badges(u: dict) -> list:
    new = []
    badges = set(u.get("badges", []))

    def give(b):
        if b not in badges:
            badges.add(b)
            new.append(b)

    if len(u.get("checkins", [])) >= 1:
        give("first-checkin")
    for st in [7, 14, 30, 60, 100]:
        if u.get("streak", 0) >= st:
            give(f"streak-{st}")
    if u.get("messages", 0) >= 1000:
        give("chatter-1000")
    if u.get("voice_seconds", 0) >= 360000:
        give("voicer-100h")
    if u.get("coins", 0) >= 5000:
        give("rich-5k")
    if u.get("kudos_received", 0) >= 20:
        give("kudos-star")
    if u.get("focus_minutes", 0) >= 120:
        give("deep-worker")

    u["badges"] = sorted(badges)
    return new


async def apply_level_roles(member: discord.Member, new_level: int):
    if not getattr(member, "guild", None):
        return
    cfg = await get_config()
    rewards = cfg.get("rewards", {})
    to_add = []
    for lvl_str, role_id in rewards.items():
        try:
            if int(lvl_str) <= new_level:
                r = member.guild.get_role(int(role_id))
                if r and r not in member.roles:
                    to_add.append(r)
        except (ValueError, TypeError):
            pass
    if to_add:
        try:
            await member.add_roles(*to_add, reason=f"Level {new_level} reward")
        except Exception as e:
            blog(f"role add failed: {e}")
