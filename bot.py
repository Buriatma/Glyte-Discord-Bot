"""Gamified office attendance + activity tracker.
MongoDB (Atlas-ready) + in-memory cache with bulk flush every 30s.
Made by GlyteTech — www.glyte.tech — info@glyte.tech 💜
"""
import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import time
import secrets
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union
from dotenv import load_dotenv
import motor.motor_asyncio
from pymongo import ReplaceOne
from bson import ObjectId
from aiohttp import web

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py3.8 fallback
    ZoneInfo = None

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "discord_bot")
XP_PER_MSG = int(os.getenv("XP_PER_MSG", "10"))
MSG_COOLDOWN_S = int(os.getenv("MSG_COOLDOWN_S", "5"))
FLUSH_EVERY_S = int(os.getenv("FLUSH_EVERY_S", "30"))
SUMMARY_CHANNEL_ID = int(os.getenv("SUMMARY_CHANNEL_ID") or 0) or None
SUMMARY_TIME = os.getenv("SUMMARY_TIME", "23:00")
SUMMARY_TZ = os.getenv("SUMMARY_TZ", "UTC")
STANDUP_CHANNEL_ID = int(os.getenv("STANDUP_CHANNEL_ID") or 0) or None
STANDUP_TIME = os.getenv("STANDUP_TIME", "10:00")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
DASHBOARD_PUBLIC_URL = os.getenv("DASHBOARD_PUBLIC_URL", f"http://localhost:{DASHBOARD_PORT}")
BASE_DIR = Path(__file__).parent

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True
intents.voice_states = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo[MONGO_DB]
users_col = db["users"]
leaves_col = db["leaves"]
config_col = db["config"]
standups_col = db["standups"]
bounties_col = db["bounties"]
dash_tokens_col = db["dashboard_tokens"]
dash_access_col = db["dashboard_access"]
stats_col = db["daily_stats"]
feedback_col = db["feedback"]

DEFAULT_USER = {
    "messages": 0,
    "xp": 0,
    "coins": 0,
    "voice_seconds": 0,
    "reactions_added": 0,
    "checkins": [],
    "voice_days": [],
    "streak": 0,
    "last_checkin": None,
    "last_daily": None,
    "badges": [],
    "inventory": [],
    "quest_claimed": {},
    "kudos_received": 0,
    "kudos_given": 0,
    "kudos_day": {},
    "seasons": {},
    "daily": {},
}

QUESTS = {
    "checkin": {"name": "✅ Daily Check-in", "target": 1, "xp": 30, "coins": 50,
                "desc": "Run /checkin today"},
    "chatter": {"name": "💬 Chatter", "target": 20, "xp": 30, "coins": 50,
                "desc": "Send 20 messages today"},
    "voicer": {"name": "🎙️ Voicer", "target": 1800, "xp": 40, "coins": 60,
               "desc": "Spend 30 min in voice today"},
    "reactor": {"name": "⭐ Reactor", "target": 5, "xp": 20, "coins": 30,
                "desc": "Add 5 reactions today"},
}

SHOP_DEFAULT = [
    {"id": "coffee", "name": "☕ Coffee Break", "cost": 200, "desc": "Redeem a coffee on the team"},
    {"id": "wfh", "name": "🏠 WFH Half-day", "cost": 1000, "desc": "Needs manager approval in real life!"},
    {"id": "earlylog", "name": "🚀 Early Logout", "cost": 500, "desc": "Leave 1h early (manager approval needed)"},
    {"id": "mvp", "name": "🏅 MVP Nomination", "cost": 800, "desc": "Nominate yourself for monthly MVP"},
]

STREAK_BONUS = {7: 100, 14: 200, 30: 500, 60: 1200, 100: 3000}

# ---- speed layer: cache + bulk flush ----
_cache: dict = {}
_dirty: set = set()
_last_msg_ts: dict = {}
voice_join: dict = {}


def today_str(tz=timezone.utc):
    return datetime.now(tz).date().isoformat()


def yesterday_str(tz=timezone.utc):
    return (datetime.now(tz).date() - timedelta(days=1)).isoformat()


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


def sched_tz():
    if ZoneInfo:
        try:
            return ZoneInfo(SUMMARY_TZ)
        except Exception:
            pass
    return timezone.utc


def season_str(dt=None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m")


def bump_season(u: dict, xp: int = 0, messages: int = 0, voice: int = 0):
    row = u.setdefault("seasons", {}).setdefault(season_str(), {"xp": 0, "messages": 0, "voice": 0})
    row["xp"] += xp
    row["messages"] += messages
    row["voice"] += voice


async def get_config() -> dict:
    doc = await config_col.find_one({"_id": "global"})
    if doc is None:
        doc = {"_id": "global", "rewards": {}, "shop": SHOP_DEFAULT}
        await config_col.insert_one(doc)
    for k, v in [("rewards", {}), ("shop", SHOP_DEFAULT)]:
        if k not in doc:
            doc[k] = v
    return doc


async def save_config(doc: dict):
    await config_col.replace_one({"_id": "global"}, doc, upsert=True)


async def get_user(uid: Union[int, str]) -> dict:
    uid = str(uid)
    if uid in _cache:
        return _cache[uid]
    doc = await users_col.find_one({"_id": uid})
    if doc is None:
        doc = {"_id": uid, **{k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                              for k, v in DEFAULT_USER.items()}}
        await users_col.insert_one(doc)
    else:
        changed = False
        for k, v in DEFAULT_USER.items():
            if k not in doc:
                doc[k] = list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v
                changed = True
        if changed:
            await users_col.replace_one({"_id": uid}, doc, upsert=True)
    _cache[uid] = doc
    return doc


def mark_dirty(uid: Union[int, str]):
    _dirty.add(str(uid))


async def flush_cache():
    if not _dirty:
        return
    ops = []
    for uid in list(_dirty):
        doc = _cache.get(uid)
        if doc is not None:
            ops.append(ReplaceOne({"_id": uid}, doc, upsert=True))
    _dirty.clear()
    if ops:
        try:
            await users_col.bulk_write(ops, ordered=False)
        except Exception as e:
            print(f"flush failed: {e}")


@tasks.loop(seconds=30)
async def autosave():
    await flush_cache()


# ---- live bot logs (ring buffer for the admin dashboard) ----
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


# ---- self-improvement: usage stats (fire-and-forget, never blocks) ----
def track(key: str, sub: Optional[str] = None):
    async def _go():
        try:
            day = today_str()
            if sub:
                await stats_col.update_one(
                    {"_id": day}, {"$inc": {f"{key}.{sub}": 1}}, upsert=True)
            else:
                await stats_col.update_one(
                    {"_id": day}, {"$inc": {key: 1}}, upsert=True)
        except Exception as e:
            print(f"track failed: {e}")
    try:
        bot.loop.create_task(_go())
    except Exception:
        pass


async def approved_leave_map():
    """user_id -> set of YYYY-MM-DD on approved leave."""
    out: dict = {}
    async for lv in leaves_col.find({"status": "approved"}):
        try:
            d0 = datetime.fromisoformat(lv["from"]).date()
        except Exception:
            continue
        days = out.setdefault(str(lv["user_id"]), set())
        for i in range(int(lv.get("days", 1))):
            days.add((d0 + timedelta(days=i)).isoformat())
    return out


async def build_eod(guild: discord.Guild, day: str) -> discord.Embed:
    await flush_cache()
    docs = {d["_id"]: d async for d in users_col.find({})}
    on_leave = await approved_leave_map()
    members = [m for m in guild.members if not m.bot]
    present, voice_only, leaves, absent = [], [], [], []
    for m in members:
        uid = str(m.id)
        u = docs.get(uid)
        if u and day in u.get("checkins", []):
            present.append(m.display_name)
        elif u and day in u.get("voice_days", []):
            voice_only.append(m.display_name)
        elif day in on_leave.get(uid, set()):
            leaves.append(m.display_name)
        else:
            absent.append(m.display_name)

    def fmt(names, cap=20):
        if not names:
            return "—"
        s = ", ".join(names[:cap])
        if len(names) > cap:
            s += f" …+{len(names) - cap} more"
        return s

    total = len(members) or 1
    pct = round(100 * (len(present) + len(voice_only)) / total)
    embed = discord.Embed(title=f"🌙 EOD Summary — {day} · {pct}% in", color=0x5865F2)
    embed.add_field(name=f"✅ Present ({len(present)})", value=fmt(present), inline=False)
    if voice_only:
        embed.add_field(name=f"🎙️ Voice only ({len(voice_only)})", value=fmt(voice_only), inline=False)
    embed.add_field(name=f"🌴 On leave ({len(leaves)})", value=fmt(leaves), inline=False)
    embed.add_field(name=f"❌ Absent ({len(absent)})", value=fmt(absent), inline=False)
    return embed


async def award_season_champion(prev: str):
    best, best_xp = None, 0
    async for d in users_col.find({}):
        xp = d.get("seasons", {}).get(prev, {}).get("xp", 0)
        if xp > best_xp:
            best, best_xp = d, xp
    if best is None or best_xp <= 0:
        return
    u = await get_user(best["_id"])
    u["coins"] += 1000
    badge = f"season-champion-{prev}"
    if badge not in u.get("badges", []):
        u["badges"] = sorted(set(u["badges"]) | {badge})
    grant_badges(u)
    mark_dirty(best["_id"])
    await flush_cache()
    if SUMMARY_CHANNEL_ID:
        ch = bot.get_channel(SUMMARY_CHANNEL_ID)
        if ch:
            try:
                await ch.send(f"👑 **Season {prev} champion**: <@{best['_id']}> "
                              f"with **{best_xp} XP**! +1000 🪙 + exclusive badge 🏅")
            except Exception as e:
                blog(f"champion announce failed: {e}")


@tasks.loop(minutes=1)
async def scheduler():
    try:
        now = datetime.now(sched_tz())
        hm = now.strftime("%H:%M")
        today = now.date().isoformat()
        cfg = await get_config()
        changed = False
        cur = season_str()
        if cfg.get("current_season") != cur:
            prev = cfg.get("current_season")
            cfg["current_season"] = cur
            changed = True
            if prev:
                await award_season_champion(prev)
        if cfg.get("last_tune") != today:
            cfg["last_tune"] = today
            changed = True
            for a in await tune_quests(cfg):
                blog(f"🧠 tuned {a['quest']}: {a['old']}→{a['new']} (rate {a['rate']})")
        try:
            cutoff = (now - timedelta(days=30)).isoformat()
            await dash_access_col.delete_many({"at": {"$lt": cutoff}})
        except Exception:
            pass
        if SUMMARY_CHANNEL_ID and hm == SUMMARY_TIME and cfg.get("last_eod") != today:
            ch = bot.get_channel(SUMMARY_CHANNEL_ID)
            if ch and getattr(ch, "guild", None):
                try:
                    await ch.send(embed=await build_eod(ch.guild, today))
                    cfg["last_eod"] = today
                    changed = True
                except Exception as e:
                    blog(f"eod post failed: {e}")
        if STANDUP_CHANNEL_ID and hm == STANDUP_TIME and cfg.get("last_standup") != today:
            ch = bot.get_channel(STANDUP_CHANNEL_ID)
            if ch:
                try:
                    msg = await ch.send(
                        f"🧍 **Daily standup — {today}**\n"
                        f"Drop your update with `/standup` (yesterday / today / blockers) 👇")
                    try:
                        await msg.create_thread(name=f"standup-{today}",
                                                auto_archive_duration=1440)
                    except Exception:
                        pass
                    cfg["last_standup"] = today
                    changed = True
                except Exception as e:
                    print(f"standup post failed: {e}")
        if changed:
            await save_config(cfg)
    except Exception as e:
        blog(f"scheduler failed: {e}")


def grant_badges(u: dict) -> list:
    new = []
    badges = set(u.get("badges", []))

    def give(b):
        if b not in badges:
            badges.add(b)
            new.append(b)

    if len(u.get("checkins", [])) >= 1:
        give("first-checkin")
    if u.get("streak", 0) >= 7:
        give("streak-7")
    if u.get("streak", 0) >= 30:
        give("streak-30")
    if u.get("messages", 0) >= 1000:
        give("chatter-1000")
    if u.get("voice_seconds", 0) >= 360000:
        give("voicer-100h")
    if u.get("coins", 0) >= 5000:
        give("rich-5k")
    u["badges"] = sorted(badges)
    return new


async def apply_level_roles(member: discord.Member, new_level: int):
    if member is None or member.guild is None:
        return
    try:
        cfg = await get_config()
        rewards = cfg.get("rewards", {})
        role_id = rewards.get(str(new_level))
        if not role_id:
            return
        role = member.guild.get_role(int(role_id))
        if role and role not in member.roles:
            await member.add_roles(role, reason=f"Reached level {new_level}")
    except Exception as e:
        print(f"role assign failed: {e}")


def quest_progress(u: dict, day: str, targets: Optional[dict] = None) -> dict:
    d = u.get("daily", {}).get(day, {"messages": 0, "voice": 0})
    return {
        "checkin": 1 if day in u.get("checkins", []) else 0,
        "chatter": d.get("messages", 0),
        "voicer": d.get("voice", 0),
        "reactor": d.get("day_reactions", d.get("reactions", 0)),
    }


QUEST_BOUNDS = {"checkin": (1, 1), "chatter": (10, 40),
                "voicer": (900, 3600), "reactor": (2, 15)}


async def eff_targets() -> dict:
    cfg = await get_config()
    saved = cfg.get("quest_targets", {})
    return {qid: saved.get(qid, q["target"]) for qid, q in QUESTS.items()}


async def tune_quests(cfg: dict) -> list:
    """Self-tuning: nudge daily-quest targets toward ~20-80% completion."""
    end = datetime.now(timezone.utc).date()
    dates = [(end - timedelta(days=i)).isoformat() for i in range(7)]
    users = await users_col.count_documents({})
    if users == 0:
        return []
    claims = {}
    async for d in stats_col.find({"_id": {"$in": dates}}):
        for qid, n in d.get("quests", {}).items():
            claims[qid] = claims.get(qid, 0) + n
    saved = dict(cfg.get("quest_targets", {}))
    actions = []
    for qid, q in QUESTS.items():
        lo, hi = QUEST_BOUNDS[qid]
        if lo == hi:
            continue
        cur = saved.get(qid, q["target"])
        rate = claims.get(qid, 0) / (users * 7)
        new = cur
        if rate < 0.2 and cur > lo:
            new = max(lo, round(cur * 0.8))
        elif rate > 0.8 and cur < hi:
            new = min(hi, max(cur + 1, round(cur * 1.2)))
        if new != cur:
            saved[qid] = new
            actions.append({"at": datetime.now(timezone.utc).isoformat(),
                            "quest": qid, "old": cur, "new": new,
                            "rate": round(rate, 2)})
    if actions:
        cfg["quest_targets"] = saved
        cfg["tune_log"] = (cfg.get("tune_log", []) + actions)[-20:]
    return actions


async def build_insights() -> dict:
    end = datetime.now(timezone.utc).date()
    dates = [(end - timedelta(days=i)).isoformat() for i in range(7)]
    users = await users_col.count_documents({})
    claims: dict = {}
    checkins_7 = kudos_7 = standups_7 = 0
    async for d in stats_col.find({"_id": {"$in": dates}}):
        checkins_7 += d.get("checkins", 0)
        kudos_7 += d.get("kudos", 0)
        standups_7 += d.get("standups", 0)
        for qid, n in d.get("quests", {}).items():
            claims[qid] = claims.get(qid, 0) + n
    cfg = await get_config()
    saved = cfg.get("quest_targets", {})
    QR = {}
    for qid, q in QUESTS.items():
        tgt = saved.get(qid, q["target"])
        rate = (claims.get(qid, 0) / (users * 7)) if users else 0
        QR[qid] = {"name": q["name"], "target": tgt, "rate": round(rate, 2)}
    sugg = []
    for qid, r in QR.items():
        if r["rate"] < 0.2:
            sugg.append(f"💡 {r['name']} completion {int(r['rate']*100)}% — target auto-tuned to {r['target']}")
        elif r["rate"] > 0.9:
            sugg.append(f"🚀 {r['name']} too easy ({int(r['rate']*100)}%) — target raised to {r['target']}")
    if users and checkins_7 / (users * 7) < 0.5:
        sugg.append("📣 Under 50% check in daily — pin 📌 /dashboard link + morning nudge")
    if kudos_7 == 0:
        sugg.append("🙌 Zero kudos this week — managers, kick one off to spark culture")
    if standups_7 == 0:
        sugg.append("🧍 No standups logged — set STANDUP_CHANNEL_ID + time")
    fb = [d async for d in feedback_col.find({}).sort("at", -1).limit(3)]
    if fb:
        sugg.append(f"📬 {await feedback_col.count_documents({})} feedback notes — latest in 🧠 tab")
    if not sugg:
        sugg.append("🌟 Everything healthy — team is thriving!")
    return {"users": users, "checkins_7": checkins_7, "kudos_7": kudos_7,
            "standups_7": standups_7, "quests": QR,
            "tune_log": cfg.get("tune_log", [])[-5:],
            "feedback": [{"by": f["user_id"], "text": f["text"][:200]} for f in fb],
            "suggestions": sugg}


# ---- events ----
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Sync failed: {e}")
    try:
        await mongo.admin.command("ping")
        print(f"Mongo connected: {MONGO_DB}")
    except Exception as e:
        print(f"Mongo ping failed: {e}")
    for idx in ["xp", "coins", "messages", "voice_seconds", "last_checkin"]:
        try:
            await users_col.create_index(idx)
        except Exception:
            pass
    try:
        await leaves_col.create_index([("user_id", 1), ("status", 1)])
    except Exception:
        pass
    for col, keys in [(standups_col, [("user_id", 1), ("date", 1)]),
                       (bounties_col, [("status", 1)])]:
        try:
            await col.create_index(keys)
        except Exception:
            pass
    try:
        await dash_tokens_col.create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        pass
    if not autosave.is_running():
        autosave.start()
    if not scheduler.is_running():
        scheduler.start()
    await start_dashboard()


@bot.event
async def on_message(message: discord.Message):
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
        u["xp"] += XP_PER_MSG
        u["coins"] += 2  # trickle coins for chat
        bump_season(u, xp=XP_PER_MSG)
        if calc_level(u["xp"]) > old_lvl:
            gained_level = calc_level(u["xp"])
    mark_dirty(uid)
    if gained_level:
        grant_badges(u)
        await apply_level_roles(message.author, gained_level)
        try:
            await message.channel.send(
                f"🎉 {message.author.mention} leveled up to **Level {gained_level}**!"
            )
        except Exception:
            pass
    await bot.process_commands(message)


@bot.event
async def on_reaction_add(reaction, user):
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


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    uid = str(member.id)
    now = time.time()
    was_in = before.channel is not None
    is_in = after.channel is not None
    if not was_in and is_in:
        voice_join[uid] = now
    elif was_in and not is_in:
        start = voice_join.pop(uid, None)
        if start is not None:
            delta = int(now - start)
            if delta > 0:
                u = await get_user(uid)
                day = today_str()
                u["voice_seconds"] += delta
                u["xp"] += delta // 60
                u["coins"] += delta // 300  # 1 coin per 5 voice-min
                bump_season(u, xp=delta // 60, voice=delta)
                u["daily"].setdefault(day, {"messages": 0, "voice": 0, "reactions": 0})["voice"] += delta
                if day not in u["voice_days"]:
                    u["voice_days"].append(day)
                mark_dirty(uid)
    elif was_in and is_in and before.channel != after.channel:
        voice_join.setdefault(uid, now)


# ---- core commands ----
@bot.tree.command(name="checkin", description="Mark today's attendance (+XP, coins, streaks)")
async def checkin(interaction: discord.Interaction):
    await interaction.response.defer()
    u = await get_user(interaction.user.id)
    today = today_str()
    if today in u["checkins"]:
        await interaction.followup.send(
            f"✅ Already checked in today! Streak: **{u['streak']}** 🔥", ephemeral=True)
        return
    u["streak"] = u["streak"] + 1 if u["last_checkin"] == yesterday_str() else 1
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
    await flush_cache()
    new_lvl = calc_level(u["xp"])
    if new_lvl > old_lvl:
        await apply_level_roles(interaction.user, new_lvl)
    msg = (f"✅ {interaction.user.mention} checked in for **{today}**! 🔥 Streak **{u['streak']}** "
           f"(+20 XP, +25 coins)")
    if bonus:
        msg += f" 🎁 Streak bonus +{bonus} coins!"
    if new_lvl > old_lvl:
        msg += f" 🎉 Level up → **{new_lvl}**!"
    if new_badges:
        msg += f" 🏅 New badges: {', '.join(new_badges)}"
    await interaction.followup.send(msg)


@bot.tree.command(name="daily", description="Claim daily coins (every 24h window by date)")
async def daily(interaction: discord.Interaction):
    u = await get_user(interaction.user.id)
    today = today_str()
    if u.get("last_daily") == today:
        await interaction.response.send_message("⏳ Already claimed today. Come back tomorrow!",
                                                ephemeral=True)
        return
    u["last_daily"] = today
    reward = 100 + min(u.get("streak", 0), 30) * 5
    u["coins"] += reward
    mark_dirty(interaction.user.id)
    await interaction.response.send_message(f"🎁 +{reward} coins! Balance: **{u['coins']}**")


@bot.tree.command(name="mystats", description="Show your (or another member's) gamified stats")
@app_commands.describe(member="Member to look up (default: you)")
async def mystats(interaction: discord.Interaction, member: Optional[discord.Member] = None):
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
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Top members by XP, coins, messages, voice, check-ins")
@app_commands.describe(category="What to rank by", limit="How many (max 25)")
@app_commands.choices(category=[
    app_commands.Choice(name="XP", value="xp"),
    app_commands.Choice(name="Coins", value="coins"),
    app_commands.Choice(name="Messages", value="messages"),
    app_commands.Choice(name="Voice time", value="voice_seconds"),
    app_commands.Choice(name="Check-ins", value="checkins"),
])
async def leaderboard(interaction: discord.Interaction,
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


@bot.tree.command(name="quests", description="Show today's quests and progress")
async def quests(interaction: discord.Interaction):
    u = await get_user(interaction.user.id)
    day = today_str()
    targets = await eff_targets()
    prog = quest_progress(u, day)
    claimed = u.get("quest_claimed", {})
    lines = []
    for qid, q in QUESTS.items():
        tgt = targets[qid]
        p = prog.get(qid, 0)
        done = p >= tgt
        status = "✅" if done else "⬜"
        claim = " (claimed)" if claimed.get(qid) == day else ""
        unit = "" if qid in ("checkin",) else f"/{tgt}"
        lines.append(f"{status} **{q['name']}** — {min(p, tgt)}{unit} "
                     f"(+{q['xp']} XP, +{q['coins']} 🪙){claim}\n_{q['desc']}_")
    await interaction.response.send_message(embed=discord.Embed(
        title="🗺️ Today's Quests", description="\n\n".join(lines), color=0x57F287).set_footer(
        text="🎯 Targets self-tune daily to fit the team"))


@bot.tree.command(name="quest_claim", description="Claim a completed quest's reward")
@app_commands.describe(quest_id="Quest to claim: checkin, chatter, voicer, reactor")
async def quest_claim(interaction: discord.Interaction, quest_id: str):
    quest_id = quest_id.lower().strip()
    if quest_id not in QUESTS:
        await interaction.response.send_message("❌ Unknown quest. Use /quests to see IDs.",
                                                ephemeral=True)
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


@bot.tree.command(name="shop", description="Show the rewards shop")
async def shop(interaction: discord.Interaction):
    cfg = await get_config()
    items = cfg.get("shop", SHOP_DEFAULT)
    u = await get_user(interaction.user.id)
    lines = [f"`{it['id']}` **{it['name']}** — {it['cost']} 🪙\n_{it['desc']}_" for it in items]
    await interaction.response.send_message(embed=discord.Embed(
        title=f"🏪 Shop (balance: {u['coins']} 🪙)",
        description="\n\n".join(lines), color=0xEB459E))


@bot.tree.command(name="buy", description="Buy an item from the shop with coins")
@app_commands.describe(item_id="Item id from /shop")
async def buy(interaction: discord.Interaction, item_id: str):
    cfg = await get_config()
    items = {it["id"]: it for it in cfg.get("shop", SHOP_DEFAULT)}
    item = items.get(item_id.lower().strip())
    if not item:
        await interaction.response.send_message("❌ Unknown item. See /shop.", ephemeral=True)
        return
    u = await get_user(interaction.user.id)
    if u["coins"] < item["cost"]:
        await interaction.response.send_message(
            f"❌ Need {item['cost']} 🪙, you have {u['coins']}.", ephemeral=True)
        return
    u["coins"] -= item["cost"]
    u["inventory"].append({"id": item["id"], "name": item["name"], "at": today_str()})
    mark_dirty(interaction.user.id)
    await interaction.response.send_message(
        f"🛍️ {interaction.user.mention} bought **{item['name']}**! Balance: {u['coins']} 🪙")


@bot.tree.command(name="inventory", description="Show your owned items")
async def inventory(interaction: discord.Interaction):
    u = await get_user(interaction.user.id)
    inv = u.get("inventory", [])
    desc = "\n".join(f"• **{it['name']}** (`{it['id']}`, {it.get('at', '?')})" for it in inv[-20:]) or "Empty. Visit /shop!"
    await interaction.response.send_message(embed=discord.Embed(
        title=f"🎒 {interaction.user.display_name}'s Inventory", description=desc))


@bot.tree.command(name="badges", description="Show badges")
@app_commands.describe(member="Member (default: you)")
async def badges(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    u = await get_user(member.id)
    await interaction.response.send_message(embed=discord.Embed(
        title=f"🏅 {member.display_name}'s Badges",
        description=", ".join(f"`{b}`" for b in u.get("badges", [])) or "No badges yet.",
        color=0xFEE75C))


@bot.tree.command(name="attendance", description="Show attendance for last N days")
@app_commands.describe(member="Member (default: you)", days="Last N days (max 30)")
async def attendance(interaction: discord.Interaction,
                     member: Optional[discord.Member] = None, days: int = 7):
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
        v = " 🎙️" if d in voice_days else ""
        lines.append(f"{c} {d}{v}")
    await interaction.response.send_message(embed=discord.Embed(
        title=f"🗓️ {member.display_name} — {present}/{days} days",
        description="\n".join(lines), color=0x57F287).set_footer(
        text="✅ checkin · 🎙️ voice · 🌴 approved leave"))


# ---- leaves ----
@bot.tree.command(name="leave_apply", description="Apply for leave")
@app_commands.describe(days="Number of days", reason="Reason", from_date="Start YYYY-MM-DD (default today)")
async def leave_apply(interaction: discord.Interaction, days: int,
                      reason: str, from_date: Optional[str] = None):
    days = max(1, min(days, 30))
    try:
        d0 = datetime.strptime(from_date, "%Y-%m-%d").date() if from_date else datetime.now(timezone.utc).date()
    except ValueError:
        await interaction.response.send_message("from_date must be YYYY-MM-DD.", ephemeral=True)
        return
    doc = {"user_id": str(interaction.user.id), "days": days, "reason": reason,
           "from": d0.isoformat(), "status": "pending",
           "created_at": datetime.now(timezone.utc).isoformat()}
    res = await leaves_col.insert_one(doc)
    await interaction.response.send_message(
        f"📝 Leave #{res.inserted_id} for **{days}d** from {d0} recorded as pending.")


@bot.tree.command(name="my_leaves", description="Show your leave requests")
async def my_leaves(interaction: discord.Interaction):
    rows = [d async for d in leaves_col.find(
        {"user_id": str(interaction.user.id)}).sort("created_at", -1).limit(10)]
    if not rows:
        await interaction.response.send_message("No leaves yet.", ephemeral=True)
        return
    lines = [f"`{r['_id']}` {r['from']} ×{r['days']}d — **{r['status']}** — {r['reason']}" for r in rows]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="leave_list", description="List pending leaves (managers)")
async def leave_list(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
        return
    rows = [d async for d in leaves_col.find({"status": "pending"}).sort("created_at", -1).limit(15)]
    if not rows:
        await interaction.response.send_message("No pending leaves. 🎉", ephemeral=True)
        return
    lines = [f"`{r['_id']}` <@{r['user_id']}> {r['from']} ×{r['days']}d — {r['reason']}" for r in rows]
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="leave_decide", description="Approve/reject a leave (managers)")
@app_commands.describe(leave_id="Leave id from /leave_list", approve="Approve?")
async def leave_decide(interaction: discord.Interaction, leave_id: str, approve: bool):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
        return
    try:
        oid = ObjectId(leave_id)
    except Exception:
        await interaction.response.send_message("Bad leave id.", ephemeral=True)
        return
    res = await leaves_col.update_one(
        {"_id": oid, "status": "pending"},
        {"$set": {"status": "approved" if approve else "rejected",
                  "decided_by": str(interaction.user.id)}})
    if not res.modified_count:
        await interaction.response.send_message("Not found or already decided.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Leave {leave_id} {'approved' if approve else 'rejected'}.")


# ---- admin: rewards/economy ----
@bot.tree.command(name="reward_add", description="Map a level to a role (admin)")
@app_commands.describe(level="Level number", role="Role to grant")
async def reward_add(interaction: discord.Interaction, level: int, role: discord.Role):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
        return
    cfg = await get_config()
    cfg.setdefault("rewards", {})[str(level)] = role.id
    await save_config(cfg)
    await interaction.response.send_message(f"✅ Level {level} → {role.mention}")


@bot.tree.command(name="reward_list", description="Show level-role rewards")
async def reward_list(interaction: discord.Interaction):
    cfg = await get_config()
    rewards = cfg.get("rewards", {})
    if not rewards:
        await interaction.response.send_message("No level rewards yet. Use /reward_add.", ephemeral=True)
        return
    lines = [f"Lv **{lvl}** → <@&{rid}>" for lvl, rid in sorted(rewards.items(), key=lambda x: int(x[0]))]
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="give_coins", description="Give coins (admin)")
@app_commands.describe(member="Who", amount="How many")
async def give_coins(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
        return
    u = await get_user(member.id)
    u["coins"] += amount
    mark_dirty(member.id)
    await interaction.response.send_message(f"🪙 Gave {amount} coins to {member.mention}. Balance: {u['coins']}")


@bot.tree.command(name="export", description="Export data (mods only)")
async def export(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await flush_cache()
    import json
    import tempfile
    docs = [d async for d in users_col.find({})]
    for d in docs:
        d["user_id"] = str(d.pop("_id"))
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(docs, f, indent=2)
        path = f.name
    await interaction.followup.send("Mongo export:", file=discord.File(path, filename="export.json"))


# ---- rituals: EOD, standups, reports, kudos, bounties, seasons ----
@bot.tree.command(name="eod", description="Post today's EOD summary now (managers)")
async def eod(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("❌ Server only.", ephemeral=True)
        return
    await interaction.response.defer()
    await interaction.followup.send(
        embed=await build_eod(interaction.guild, today_str()))


@bot.tree.command(name="standup", description="Post your daily standup update")
@app_commands.describe(yesterday="What you did", today="What you'll do", blockers="Blockers (if any)")
async def standup(interaction: discord.Interaction,
                  yesterday: Optional[str] = None,
                  today: Optional[str] = None,
                  blockers: Optional[str] = None):
    if not any([yesterday, today, blockers]):
        await interaction.response.send_message(
            "❌ Give at least one of yesterday / today / blockers.", ephemeral=True)
        return
    day = today_str()
    await standups_col.replace_one(
        {"user_id": str(interaction.user.id), "date": day},
        {"user_id": str(interaction.user.id), "date": day,
         "yesterday": yesterday or "—", "today": today or "—",
         "blockers": blockers or "—",
         "at": datetime.now(timezone.utc).isoformat()},
        upsert=True)
    u = await get_user(interaction.user.id)
    u["xp"] += 5
    bump_season(u, xp=5)
    mark_dirty(interaction.user.id)
    track("standups")
    await interaction.response.send_message(
        f"🧍 Standup saved for **{day}**! (+5 XP)", ephemeral=True)


@bot.tree.command(name="standup_list", description="Compiled standups for a day (managers)")
@app_commands.describe(date="YYYY-MM-DD (default today)")
async def standup_list(interaction: discord.Interaction, date: Optional[str] = None):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
        return
    day = date or today_str()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        await interaction.response.send_message("Use YYYY-MM-DD.", ephemeral=True)
        return
    rows = [d async for d in standups_col.find({"date": day}).sort("at", 1)]
    if not rows:
        await interaction.response.send_message(f"No standups for {day} yet. 🧍", ephemeral=True)
        return
    lines = []
    for r in rows:
        name = f"<@{r['user_id']}>"
        if interaction.guild:
            m = interaction.guild.get_member(int(r["user_id"]))
            if m:
                name = m.display_name
        lines.append(f"**{name}**\n↩️ {r['yesterday']}\n➡️ {r['today']}\n🚧 {r['blockers']}")
    await interaction.response.send_message(embed=discord.Embed(
        title=f"🧍 Standups — {day} ({len(rows)})",
        description="\n\n".join(lines)[:4000], color=0x57F287))


@bot.tree.command(name="report", description="Weekly activity report as CSV (managers)")
@app_commands.describe(days="Last N days (default 7, max 31)")
async def report(interaction: discord.Interaction, days: int = 7):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    import csv
    import tempfile
    days = max(1, min(days, 31))
    await flush_cache()
    end = datetime.now(timezone.utc).date()
    dates = [(end - timedelta(days=i)).isoformat() for i in range(days)]
    date_set = set(dates)
    on_leave = await approved_leave_map()
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
    tracked = len(rows) or 1
    embed = discord.Embed(title=f"📈 Report — last {days}d ({dates[-1]} → {dates[0]})",
                          color=0x5865F2)
    embed.add_field(name="👥 Tracked", value=str(len(rows)), inline=True)
    embed.add_field(name="✅ Avg check-ins/day",
                    value=str(round(total_check / days, 1)), inline=True)
    embed.add_field(name="💬 Top chatter", value=f"{top_msg[0]} ({top_msg[1]})", inline=True)
    embed.add_field(name="🎙️ Top voice", value=f"{top_voice[0]} ({fmt_duration(top_voice[1])})",
                    inline=True)
    await interaction.followup.send(embed=embed,
                                    file=discord.File(path, filename=f"report-{days}d.csv"))


@bot.tree.command(name="kudos", description="Give a public shout-out + coin tip (3/day)")
@app_commands.describe(member="Who to appreciate", reason="Why they rock")
async def kudos(interaction: discord.Interaction, member: discord.Member, reason: str):
    if member.id == interaction.user.id:
        await interaction.response.send_message("❌ No self-kudos. 😏", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("❌ Bots don't need kudos. 🤖", ephemeral=True)
        return
    g = await get_user(interaction.user.id)
    day = today_str()
    kd = g.setdefault("kudos_day", {})
    if kd.get(day, 0) >= 3:
        await interaction.response.send_message("⏳ Kudos limit reached (3/day).", ephemeral=True)
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
    await interaction.response.send_message(embed=discord.Embed(
        title="🙌 Kudos!",
        description=f"{interaction.user.mention} → {member.mention} (+20 🪙)\n> {reason}",
        color=0xFEE75C))


@bot.tree.command(name="bounty_post", description="Post a paid task (coins escrowed, managers)")
@app_commands.describe(title="Task title", coins="Reward coins", description="Details")
async def bounty_post(interaction: discord.Interaction, title: str, coins: int,
                      description: Optional[str] = None):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
        return
    if coins <= 0:
        await interaction.response.send_message("Coins must be positive.", ephemeral=True)
        return
    u = await get_user(interaction.user.id)
    if u["coins"] < coins:
        await interaction.response.send_message(
            f"❌ You have {u['coins']} 🪙, need {coins}.", ephemeral=True)
        return
    u["coins"] -= coins
    mark_dirty(interaction.user.id)
    res = await bounties_col.insert_one({
        "title": title, "description": description or "—", "coins": coins,
        "poster_id": str(interaction.user.id), "status": "open", "claimer_id": None,
        "created_at": datetime.now(timezone.utc).isoformat()})
    await interaction.response.send_message(
        f"🎯 Bounty posted! `{res.inserted_id}` **{title}** — {coins} 🪙 (escrowed)")


@bot.tree.command(name="bounty_list", description="Show open bounties")
async def bounty_list(interaction: discord.Interaction):
    rows = [d async for d in bounties_col.find(
        {"status": {"$in": ["open", "claimed"]}}).sort("created_at", -1).limit(10)]
    if not rows:
        await interaction.response.send_message("No open bounties. 🎯", ephemeral=True)
        return
    lines = []
    for r in rows:
        extra = f" · claimed by <@{r['claimer_id']}>" if r["status"] == "claimed" else ""
        lines.append(f"`{r['_id']}` **{r['title']}** — {r['coins']} 🪙 · *{r['status']}*{extra}"
                     f"\n_{r['description']}_")
    await interaction.response.send_message("\n\n".join(lines))


@bot.tree.command(name="bounty_claim", description="Claim an open bounty task")
@app_commands.describe(bounty_id="Bounty id from /bounty_list")
async def bounty_claim(interaction: discord.Interaction, bounty_id: str):
    try:
        oid = ObjectId(bounty_id)
    except Exception:
        await interaction.response.send_message("Bad bounty id.", ephemeral=True)
        return
    doc = await bounties_col.find_one({"_id": oid})
    if not doc or doc["status"] != "open":
        await interaction.response.send_message("Not open.", ephemeral=True)
        return
    if doc["poster_id"] == str(interaction.user.id):
        await interaction.response.send_message("❌ Can't claim your own bounty.", ephemeral=True)
        return
    await bounties_col.update_one(
        {"_id": oid, "status": "open"},
        {"$set": {"status": "claimed", "claimer_id": str(interaction.user.id)}})
    await interaction.response.send_message(
        f"🤝 {interaction.user.mention} claimed **{doc['title']}**! Poster approves with `/bounty_approve`.")


@bot.tree.command(name="bounty_approve", description="Approve work + pay the claimer (poster/managers)")
@app_commands.describe(bounty_id="Bounty id")
async def bounty_approve(interaction: discord.Interaction, bounty_id: str):
    try:
        oid = ObjectId(bounty_id)
    except Exception:
        await interaction.response.send_message("Bad bounty id.", ephemeral=True)
        return
    doc = await bounties_col.find_one({"_id": oid})
    if not doc or doc["status"] != "claimed":
        await interaction.response.send_message("Nothing to approve.", ephemeral=True)
        return
    is_poster = doc["poster_id"] == str(interaction.user.id)
    if not (is_poster or interaction.user.guild_permissions.manage_guild):
        await interaction.response.send_message("❌ Only the poster or a manager.", ephemeral=True)
        return
    await bounties_col.update_one({"_id": oid}, {"$set": {"status": "done"}})
    u = await get_user(doc["claimer_id"])
    u["coins"] += doc["coins"]
    u["xp"] += 25
    bump_season(u, xp=25)
    grant_badges(u)
    mark_dirty(doc["claimer_id"])
    await interaction.response.send_message(
        f"💰 Paid **{doc['coins']}** 🪙 to <@{doc['claimer_id']}> for **{doc['title']}**! (+25 XP)")


@bot.tree.command(name="season", description="Monthly season leaderboard")
@app_commands.describe(month="YYYY-MM (default current)")
async def season(interaction: discord.Interaction, month: Optional[str] = None):
    await interaction.response.defer()
    month = month or season_str()
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        await interaction.followup.send("Use YYYY-MM.", ephemeral=True)
        return
    rows = []
    async for d in users_col.find({}):
        s = d.get("seasons", {}).get(month, {})
        if s.get("xp", 0) > 0 or s.get("messages", 0) > 0:
            rows.append((d["_id"], s.get("xp", 0), s.get("messages", 0),
                         s.get("voice", 0)))
    rows.sort(key=lambda r: r[1], reverse=True)
    lines = []
    for i, (uid, xp, msgs, voice) in enumerate(rows[:10], 1):
        name = f"<@{uid}>"
        if interaction.guild:
            m = interaction.guild.get_member(int(uid))
            if m:
                name = m.display_name
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`{i}.`")
        lines.append(f"{medal} **{name}** — {xp} XP · {msgs} 💬 · {fmt_duration(voice)} 🎙️")
    champ = rows[0] if rows else None
    desc = "\n".join(lines) if lines else "No activity this season yet."
    if champ and month != season_str():
        desc += f"\n\n👑 Champion: <@{champ[0]}> (+1000 🪙 + badge)"
    await interaction.followup.send(embed=discord.Embed(
        title=f"🏁 Season {month}",
        description=desc, color=0xEB459E).set_footer(
        text="Monthly race · winner gets 1000 coins + 👑 badge"))


# ---- web dashboard (magic links + JSON API) ----
_dash_runner = None


def _dash_guild():
    for cid in (SUMMARY_CHANNEL_ID, STANDUP_CHANNEL_ID):
        if cid:
            ch = bot.get_channel(cid)
            if ch and getattr(ch, "guild", None):
                return ch.guild
    return bot.guilds[0] if bot.guilds else None


def _dash_names():
    g = _dash_guild()
    names = {}
    if g:
        for m in g.members:
            names[str(m.id)] = m.display_name
    return names


async def _dash_auth(request):
    token = request.query.get("token", "")
    if not token:
        return None
    doc = await dash_tokens_col.find_one({"token": token})
    if not doc:
        return None
    try:
        exp = datetime.fromisoformat(doc["expires_at"])
    except Exception:
        return None
    if datetime.now(timezone.utc) > exp:
        await dash_tokens_col.delete_one({"token": token})
        return None
    return doc


def _need_auth(handler):
    async def wrapper(request):
        doc = await _dash_auth(request)
        if not doc:
            return web.json_response(
                {"ok": False, "error": "bad or expired link — run /dashboard again 🔄"},
                status=401)
        request["dash_user"] = doc
        return await handler(request)
    return wrapper


def _need_admin(handler):
    async def wrapper(request):
        doc = await _dash_auth(request)
        if not doc:
            return web.json_response(
                {"ok": False, "error": "bad or expired link — run /dashboard again 🔄"},
                status=401)
        if not doc.get("is_admin"):
            return web.json_response(
                {"ok": False, "error": "admins only 👑"}, status=403)
        request["dash_user"] = doc
        return await handler(request)
    return wrapper


async def dash_page(request):
    doc = await _dash_auth(request)
    if not doc:
        return web.Response(
            text="🔒 Bad or expired link — run /dashboard in Discord for a fresh one ✨",
            status=401)
    try:
        await dash_access_col.insert_one({
            "user_id": doc["user_id"], "name": doc.get("user_name", "?"),
            "at": datetime.now(timezone.utc).isoformat(), "ip": request.remote})
        await dash_tokens_col.update_one({"token": doc["token"]}, {"$inc": {"uses": 1}})
    except Exception:
        pass
    path = BASE_DIR / "dashboard.html"
    if not path.exists():
        return web.Response(text="dashboard.html missing", status=500)
    return web.FileResponse(path)


@_need_auth
async def api_overview(request):
    await flush_cache()
    today = today_str()
    docs = [d async for d in users_col.find({})]
    end = datetime.now(timezone.utc).date()
    dates = [(end - timedelta(days=i)).isoformat() for i in range(7)]
    series = []
    for d in reversed(dates):
        msgs = sum(x.get("daily", {}).get(d, {}).get("messages", 0) for x in docs)
        voice = sum(x.get("daily", {}).get(d, {}).get("voice", 0) for x in docs)
        series.append({"date": d[5:], "messages": msgs, "voice_min": round(voice / 60)})
    present = sum(1 for x in docs if today in x.get("checkins", []))
    voice_only = sum(1 for x in docs
                     if today in x.get("voice_days", []) and today not in x.get("checkins", []))
    pending = await leaves_col.count_documents({"status": "pending"})
    coins = sum(x.get("coins", 0) for x in docs)
    return web.json_response({"ok": True, "today": today, "members": len(docs),
                              "present": present, "voice_only": voice_only,
                              "pending_leaves": pending, "coins": coins, "series": series})


@_need_auth
async def api_leaderboard(request):
    key = request.query.get("key", "xp")
    limit = max(1, min(int(request.query.get("limit", "10") or 10), 25))
    names = _dash_names()
    if key == "checkins":
        cur = users_col.aggregate([
            {"$project": {"score": {"$size": {"$ifNull": ["$checkins", []]}}}},
            {"$sort": {"score": -1}}, {"$limit": limit}])
        rows = [{"name": names.get(d["_id"], d["_id"]), "score": d["score"]} async for d in cur]
    else:
        cur = users_col.find({}, {"_id": 1, key: 1}).sort(key, -1).limit(limit)
        rows = [{"name": names.get(d["_id"], d["_id"]), "score": d.get(key, 0)} async for d in cur]
    return web.json_response({"ok": True, "rows": rows})


@_need_auth
async def api_season(request):
    month = request.query.get("month") or season_str()
    names = _dash_names()
    rows = []
    async for d in users_col.find({}):
        s = d.get("seasons", {}).get(month, {})
        if s.get("xp", 0) > 0 or s.get("messages", 0) > 0:
            rows.append({"name": names.get(d["_id"], d["_id"]), "xp": s.get("xp", 0),
                         "messages": s.get("messages", 0), "voice_min": round(s.get("voice", 0) / 60)})
    rows.sort(key=lambda r: r["xp"], reverse=True)
    return web.json_response({"ok": True, "month": month, "rows": rows[:10]})


@_need_auth
async def api_attendance(request):
    days = max(1, min(int(request.query.get("days", "7") or 7), 31))
    await flush_cache()
    end = datetime.now(timezone.utc).date()
    dates = [(end - timedelta(days=i)).isoformat() for i in range(days)]
    date_set = set(dates)
    on_leave = await approved_leave_map()
    names = _dash_names()
    rows = []
    async for d in users_col.find({}):
        uid = str(d["_id"])
        checks = sorted(set(d.get("checkins", [])) & date_set, reverse=True)
        voice = sorted(set(d.get("voice_days", [])) & date_set, reverse=True)
        rows.append({"id": uid, "name": names.get(uid, uid),
                     "present": len(set(checks) | set(voice)),
                     "checkins": len(checks), "voice_days": len(voice),
                     "leaves": len(date_set & on_leave.get(uid, set())),
                     "streak": d.get("streak", 0)})
    rows.sort(key=lambda r: r["present"], reverse=True)
    return web.json_response({"ok": True, "days": days, "dates": dates, "rows": rows})


@_need_auth
async def api_members(request):
    names = _dash_names()
    return web.json_response({"ok": True,
                              "rows": [{"id": k, "name": v} for k, v in sorted(names.items())]})


@_need_auth
async def api_leaves(request):
    status = request.query.get("status", "pending")
    q = {} if status == "all" else {"status": status}
    names = _dash_names()
    rows = [d async for d in leaves_col.find(q).sort("created_at", -1).limit(50)]
    out = [{"id": str(r["_id"]), "name": names.get(str(r["user_id"]), str(r["user_id"])),
            "from": r["from"], "days": r.get("days", 1), "reason": r.get("reason", ""),
            "status": r.get("status", "")} for r in rows]
    return web.json_response({"ok": True, "rows": out})


@_need_admin
async def api_leave_decide(request):
    body = await request.json()
    try:
        oid = ObjectId(str(body.get("id", "")))
    except Exception:
        return web.json_response({"ok": False, "error": "bad id"}, status=400)
    status = "approved" if body.get("approve") else "rejected"
    res = await leaves_col.update_one({"_id": oid, "status": "pending"},
                                      {"$set": {"status": status}})
    if not res.modified_count:
        return web.json_response({"ok": False, "error": "not found / already decided"})
    return web.json_response({"ok": True, "status": status})


@_need_auth
async def api_shop(request):
    cfg = await get_config()
    return web.json_response({"ok": True, "rows": cfg.get("shop", SHOP_DEFAULT)})


@_need_admin
async def api_shop_add(request):
    body = await request.json()
    item = {"id": str(body.get("id", "")).lower().strip(),
            "name": body.get("name", ""), "cost": int(body.get("cost", 0) or 0),
            "desc": body.get("description", "")}
    if not item["id"] or not item["name"] or item["cost"] <= 0:
        return web.json_response({"ok": False, "error": "id, name + positive cost needed"})
    cfg = await get_config()
    shop = [x for x in cfg.get("shop", SHOP_DEFAULT) if x["id"] != item["id"]]
    shop.append(item)
    cfg["shop"] = shop
    await save_config(cfg)
    return web.json_response({"ok": True})


@_need_admin
async def api_shop_delete(request):
    body = await request.json()
    cfg = await get_config()
    cfg["shop"] = [x for x in cfg.get("shop", SHOP_DEFAULT) if x["id"] != body.get("id")]
    await save_config(cfg)
    return web.json_response({"ok": True})


@_need_auth
async def api_rewards(request):
    cfg = await get_config()
    return web.json_response({"ok": True, "rows": cfg.get("rewards", {})})


@_need_admin
async def api_reward_set(request):
    body = await request.json()
    cfg = await get_config()
    cfg.setdefault("rewards", {})[str(int(body.get("level", 0) or 0))] = str(body.get("role_id", ""))
    await save_config(cfg)
    return web.json_response({"ok": True})


@_need_admin
async def api_reward_delete(request):
    body = await request.json()
    cfg = await get_config()
    cfg.get("rewards", {}).pop(str(body.get("level", "")), None)
    await save_config(cfg)
    return web.json_response({"ok": True})


@_need_auth
async def api_bounties(request):
    rows = [d async for d in bounties_col.find({}).sort("created_at", -1).limit(30)]
    names = _dash_names()
    out = [{"id": str(r["_id"]), "title": r["title"], "coins": r.get("coins", 0),
            "description": r.get("description", ""), "status": r.get("status", ""),
            "claimer": names.get(str(r.get("claimer_id") or ""), "")} for r in rows]
    return web.json_response({"ok": True, "rows": out})


@_need_admin
async def api_bounty_post(request):
    body = await request.json()
    coins = int(body.get("coins", 0) or 0)
    if not body.get("title") or coins <= 0:
        return web.json_response({"ok": False, "error": "title + positive coins needed"})
    poster = request["dash_user"]["created_by"]
    u = await get_user(poster)
    if u["coins"] < coins:
        return web.json_response({"ok": False, "error": f"only {u['coins']} 🪙 available"})
    u["coins"] -= coins
    mark_dirty(poster)
    res = await bounties_col.insert_one({
        "title": body["title"], "description": body.get("description", ""), "coins": coins,
        "poster_id": poster, "status": "open", "claimer_id": None,
        "created_at": datetime.now(timezone.utc).isoformat()})
    return web.json_response({"ok": True, "id": str(res.inserted_id)})


@_need_admin
async def api_bounty_approve(request):
    body = await request.json()
    try:
        oid = ObjectId(str(body.get("id", "")))
    except Exception:
        return web.json_response({"ok": False, "error": "bad id"}, status=400)
    doc = await bounties_col.find_one({"_id": oid})
    if not doc or doc.get("status") != "claimed" or not doc.get("claimer_id"):
        return web.json_response({"ok": False, "error": "nothing to approve"})
    await bounties_col.update_one({"_id": oid}, {"$set": {"status": "done"}})
    u = await get_user(doc["claimer_id"])
    u["coins"] += doc["coins"]
    u["xp"] += 25
    bump_season(u, xp=25)
    mark_dirty(doc["claimer_id"])
    return web.json_response({"ok": True})


@_need_admin
async def api_give_coins(request):
    body = await request.json()
    amount = int(body.get("amount", 0) or 0)
    if not body.get("user_id") or amount == 0:
        return web.json_response({"ok": False, "error": "member + non-zero amount needed"})
    u = await get_user(body["user_id"])
    u["coins"] += amount
    mark_dirty(body["user_id"])
    return web.json_response({"ok": True, "balance": u["coins"]})


@_need_admin
async def api_eod(request):
    g = _dash_guild()
    if not g:
        return web.json_response({"ok": False, "error": "no server found"})
    target = None
    if SUMMARY_CHANNEL_ID:
        target = bot.get_channel(SUMMARY_CHANNEL_ID)
    if target is None:
        return web.json_response({"ok": False, "error": "set SUMMARY_CHANNEL_ID first"})
    await target.send(embed=await build_eod(g, today_str()))
    return web.json_response({"ok": True})


@_need_auth
async def api_standups(request):
    day = request.query.get("date") or today_str()
    names = _dash_names()
    rows = [d async for d in standups_col.find({"date": day}).sort("at", 1)]
    out = [{"name": names.get(str(r["user_id"]), str(r["user_id"])),
            "yesterday": r.get("yesterday", "—"), "today": r.get("today", "—"),
            "blockers": r.get("blockers", "—")} for r in rows]
    return web.json_response({"ok": True, "date": day, "rows": out})


@_need_auth
async def api_me(request):
    uid = request["dash_user"]["user_id"]
    doc = request["dash_user"]
    u = await get_user(uid)
    lvl, into, need = xp_into_level(u["xp"])
    day = today_str()
    targets = await eff_targets()
    prog = quest_progress(u, day)
    claimed = u.get("quest_claimed", {})
    quests = [{"id": qid, "name": q["name"], "desc": q["desc"], "target": targets[qid],
               "progress": min(prog.get(qid, 0), targets[qid]),
               "done": prog.get(qid, 0) >= targets[qid],
               "claimed": claimed.get(qid) == day,
               "xp": q["xp"], "coins": q["coins"]} for qid, q in QUESTS.items()]
    names = _dash_names()
    return web.json_response({
        "ok": True, "id": uid, "name": names.get(uid, doc.get("user_name", "you")),
        "is_admin": bool(doc.get("is_admin")), "level": lvl, "xp": u["xp"],
        "into": into, "need": need, "coins": u["coins"], "streak": u["streak"],
        "messages": u["messages"], "voice_seconds": u["voice_seconds"],
        "checkins": len(u.get("checkins", [])), "badges": u.get("badges", []),
        "kudos": u.get("kudos_received", 0),
        "season_xp": u.get("seasons", {}).get(season_str(), {}).get("xp", 0),
        "quests": quests})


@_need_auth
async def api_my_attendance(request):
    uid = request["dash_user"]["user_id"]
    days = max(1, min(int(request.query.get("days", "14") or 14), 31))
    u = await get_user(uid)
    end = datetime.now(timezone.utc).date()
    checkins = set(u.get("checkins", []))
    voice_days = set(u.get("voice_days", []))
    on_leave = (await approved_leave_map()).get(uid, set())
    lines, present = [], 0
    for i in range(days):
        d = (end - timedelta(days=i)).isoformat()
        here = d in checkins or d in voice_days
        present += 1 if here else 0
        mark = "✅" if d in checkins else ("🌴" if d in on_leave else ("🎙️" if d in voice_days else "⬜"))
        lines.append({"date": d, "mark": mark})
    return web.json_response({"ok": True, "present": present, "days": days, "lines": lines})


@_need_auth
async def api_quest_claim_self(request):
    body = await request.json()
    qid = str(body.get("quest_id", "")).lower().strip()
    if qid not in QUESTS:
        return web.json_response({"ok": False, "error": "unknown quest"})
    uid = request["dash_user"]["user_id"]
    u = await get_user(uid)
    day = today_str()
    if u.get("quest_claimed", {}).get(qid) == day:
        return web.json_response({"ok": False, "error": "already claimed"})
    targets = await eff_targets()
    if quest_progress(u, day).get(qid, 0) < targets[qid]:
        return web.json_response({"ok": False, "error": "not complete yet"})
    q = QUESTS[qid]
    u["xp"] += q["xp"]
    u["coins"] += q["coins"]
    bump_season(u, xp=q["xp"])
    u["quest_claimed"][qid] = day
    mark_dirty(uid)
    track("quests", qid)
    return web.json_response({"ok": True, "xp": q["xp"], "coins": q["coins"]})


@_need_auth
async def api_buy_self(request):
    body = await request.json()
    cfg = await get_config()
    items = {it["id"]: it for it in cfg.get("shop", SHOP_DEFAULT)}
    item = items.get(str(body.get("item_id", "")).lower().strip())
    if not item:
        return web.json_response({"ok": False, "error": "unknown item"})
    uid = request["dash_user"]["user_id"]
    u = await get_user(uid)
    if u["coins"] < item["cost"]:
        return web.json_response({"ok": False, "error": f"need {item['cost']} 🪙"})
    u["coins"] -= item["cost"]
    u["inventory"].append({"id": item["id"], "name": item["name"], "at": today_str()})
    mark_dirty(uid)
    track("commands", "buy")
    return web.json_response({"ok": True, "balance": u["coins"]})


@_need_auth
async def api_my_inventory(request):
    u = await get_user(request["dash_user"]["user_id"])
    return web.json_response({"ok": True, "rows": u.get("inventory", [])})


@_need_auth
async def api_my_leaves(request):
    uid = request["dash_user"]["user_id"]
    rows = [d async for d in leaves_col.find({"user_id": uid}).sort("created_at", -1).limit(10)]
    return web.json_response({"ok": True, "rows": [
        {"id": str(r["_id"]), "from": r["from"], "days": r.get("days", 1),
         "reason": r.get("reason", ""), "status": r.get("status", "")} for r in rows]})


@_need_auth
async def api_leave_apply_self(request):
    body = await request.json()
    days = max(1, min(int(body.get("days", 1) or 1), 30))
    try:
        d0 = datetime.strptime(body.get("from") or today_str(), "%Y-%m-%d").date()
    except ValueError:
        return web.json_response({"ok": False, "error": "from must be YYYY-MM-DD"})
    uid = request["dash_user"]["user_id"]
    res = await leaves_col.insert_one({
        "user_id": uid, "days": days, "reason": str(body.get("reason", ""))[:300],
        "from": d0.isoformat(), "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat()})
    return web.json_response({"ok": True, "id": str(res.inserted_id)})


@_need_admin
async def api_bot_logs(request):
    n = max(1, min(int(request.query.get("n", "150") or 150), 300))
    return web.json_response({"ok": True, "rows": list(_bot_logs)[-n:]})


@_need_admin
async def api_access_log(request):
    rows = [d async for d in dash_access_col.find({}).sort("at", -1).limit(100)]
    names = _dash_names()
    out = [{"name": names.get(str(r.get("user_id")), r.get("name", "?")),
            "at": r.get("at", ""), "ip": r.get("ip", "")} for r in rows]
    return web.json_response({"ok": True, "rows": out})


@_need_admin
async def api_insights(request):
    ins = await build_insights()
    names = _dash_names()
    for f in ins["feedback"]:
        f["by"] = names.get(str(f["by"]), str(f["by"]))
    return web.json_response({"ok": True, **ins})


async def start_dashboard():
    global _dash_runner
    if _dash_runner is not None:
        return
    app = web.Application()
    app.router.add_get("/dash/", dash_page)
    app.router.add_get("/api/overview", api_overview)
    app.router.add_get("/api/leaderboard", api_leaderboard)
    app.router.add_get("/api/season", api_season)
    app.router.add_get("/api/attendance", api_attendance)
    app.router.add_get("/api/members", api_members)
    app.router.add_get("/api/leaves", api_leaves)
    app.router.add_post("/api/leave_decide", api_leave_decide)
    app.router.add_get("/api/shop", api_shop)
    app.router.add_post("/api/shop_add", api_shop_add)
    app.router.add_post("/api/shop_delete", api_shop_delete)
    app.router.add_get("/api/rewards", api_rewards)
    app.router.add_post("/api/reward_set", api_reward_set)
    app.router.add_post("/api/reward_delete", api_reward_delete)
    app.router.add_get("/api/bounties", api_bounties)
    app.router.add_post("/api/bounty_post", api_bounty_post)
    app.router.add_post("/api/bounty_approve", api_bounty_approve)
    app.router.add_post("/api/give_coins", api_give_coins)
    app.router.add_post("/api/eod", api_eod)
    app.router.add_get("/api/standups", api_standups)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/my_attendance", api_my_attendance)
    app.router.add_post("/api/quest_claim_self", api_quest_claim_self)
    app.router.add_post("/api/buy_self", api_buy_self)
    app.router.add_get("/api/my_inventory", api_my_inventory)
    app.router.add_get("/api/my_leaves", api_my_leaves)
    app.router.add_post("/api/leave_apply_self", api_leave_apply_self)
    app.router.add_get("/api/bot_logs", api_bot_logs)
    app.router.add_get("/api/access_log", api_access_log)
    app.router.add_get("/api/insights", api_insights)
    _dash_runner = web.AppRunner(app)
    await _dash_runner.setup()
    await web.TCPSite(_dash_runner, "0.0.0.0", DASHBOARD_PORT).start()
    blog(f"📊 Dashboard live on :{DASHBOARD_PORT}")


@bot.tree.command(name="dashboard", description="Your private dashboard magic link (only you see it)")
@app_commands.describe(hours="Link validity in hours (default 24, max 72)")
async def dashboard_cmd(interaction: discord.Interaction, hours: int = 24):
    hours = max(1, min(hours, 72))
    uid = str(interaction.user.id)
    is_admin = bool(interaction.user.guild_permissions.manage_guild)
    await dash_tokens_col.delete_many({"user_id": uid})
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    await dash_tokens_col.insert_one({
        "token": token, "user_id": uid, "created_by": uid,
        "user_name": interaction.user.display_name,
        "is_admin": is_admin,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=hours)).isoformat()})
    track("commands", "dashboard")
    blog(f"📊 dashboard link for {interaction.user.display_name} (admin={is_admin})")
    role = "👑 Admin control room" if is_admin else "🙋 Your personal hub"
    link = f"{DASHBOARD_PUBLIC_URL.rstrip('/')}/dash/?token={token}"
    await interaction.response.send_message(
        f"📊 **Your magic dashboard link** ✨ — {role}, auto-logged-in as you\n{link}\n"
        f"⏳ Valid **{hours}h** · only this link knows it's you 🔒",
        ephemeral=True)


@bot.tree.command(name="insights", description="How the team is doing + bot self-tuning (managers)")
async def insights(interaction: discord.Interaction):
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


@bot.tree.command(name="feedback", description="Teach the bot — suggest or rate something")
@app_commands.describe(text="Your idea, praise, or complaint")
async def feedback(interaction: discord.Interaction, text: str):
    await feedback_col.insert_one({"user_id": str(interaction.user.id),
                                   "text": text[:500],
                                   "at": datetime.now(timezone.utc).isoformat()})
    track("commands", "feedback")
    await interaction.response.send_message(
        "📬 Noted! The bot learns from this 🧠✨", ephemeral=True)


@bot.tree.command(name="about", description="Who made this bot? 💜")
async def about(interaction: discord.Interaction):
    track("commands", "about")
    await interaction.response.send_message(embed=discord.Embed(
        title="🤖 Glyte Discord Bot",
        description="Attendance · Activity · Gamification for flexible teams ✨",
        color=0x5865F2).add_field(
        name="🏢 Made by",
        value="**GlyteTech** 💜\n🌐 www.glyte.tech\n📧 info@glyte.tech",
        inline=False))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN not set.")
    bot.run(TOKEN)
