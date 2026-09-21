"""Gamified office attendance + activity tracker.
MongoDB (Atlas-ready) + in-memory cache with bulk flush every 30s.
Made by GlyteTech — www.glyte.tech — info@glyte.tech 💜
"""
import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import os
import time
import random
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
TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
MONGO_URI = (os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017").strip()
MONGO_DB = (os.getenv("MONGO_DB") or os.getenv("MONGO_DATABASE") or os.getenv("MONGODB_DATABASE") or "discord_bot").strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except (ValueError, TypeError):
        print(f"⚠️ bad {name}, using {default}")
        return default


XP_PER_MSG = _env_int("XP_PER_MSG", 10)
MSG_COOLDOWN_S = _env_int("MSG_COOLDOWN_S", 5)
FLUSH_EVERY_S = _env_int("FLUSH_EVERY_S", 30)
SUMMARY_CHANNEL_ID = _env_int("SUMMARY_CHANNEL_ID", 0) or None
SUMMARY_TIME = os.getenv("SUMMARY_TIME", "23:00")
SUMMARY_TZ = os.getenv("SUMMARY_TZ", "UTC")
STANDUP_CHANNEL_ID = _env_int("STANDUP_CHANNEL_ID", 0) or None
STANDUP_TIME = os.getenv("STANDUP_TIME", "10:00")
DASHBOARD_PORT = _env_int("DASHBOARD_PORT", 8080)


def _resolve_public_url() -> str:
    env_url = (os.getenv("DASHBOARD_PUBLIC_URL") or "").strip()
    if env_url:
        return env_url
    try:
        import urllib.request
        with urllib.request.urlopen("https://api.ipify.org", timeout=2.0) as resp:
            ip = resp.read().decode("utf-8").strip()
            if ip:
                return f"http://{ip}:{DASHBOARD_PORT}"
    except Exception:
        pass
    return f"http://localhost:{DASHBOARD_PORT}"


DASHBOARD_PUBLIC_URL = _resolve_public_url()
BASE_DIR = Path(__file__).parent

intents = discord.Intents.default()
intents.reactions = True
intents.voice_states = True
intents.guilds = True
intents.messages = True
# Privileged intents: opt-in via env so bot can run even if not enabled in Discord Dev Portal
if (os.getenv("INTENTS_MEMBERS") or "false").lower() in ("true", "1", "yes"):
    intents.members = True
if (os.getenv("INTENTS_MESSAGE_CONTENT") or "false").lower() in ("true", "1", "yes"):
    intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

try:
    import certifi
    ca_file = certifi.where()
except ImportError:
    ca_file = None

mongo_kwargs = {
    "serverSelectionTimeoutMS": 8000,
    "connectTimeoutMS": 8000,
    "socketTimeoutMS": 20000,
    "minPoolSize": 10,
    "maxPoolSize": 50,
    "maxIdleTimeMS": 45000,
}
if ca_file and ("mongodb+srv://" in MONGO_URI or "ssl=true" in MONGO_URI.lower() or "tls=true" in MONGO_URI.lower()):
    mongo_kwargs["tlsCAFile"] = ca_file

mongo = motor.motor_asyncio.AsyncIOMotorClient(
    MONGO_URI,
    **mongo_kwargs
)
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
strikes_col = db["strikes"]

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
    "last_spin": None,
    "focus_minutes": 0,
    "boost_xp_until": 0,
    "birthday": None,
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
    {"id": "freeze", "name": "🧊 Streak Freeze", "cost": 300, "emoji": "🧊", "desc": "Protects your checkin streak if you miss a day!"},
    {"id": "xp2x", "name": "⚡ 2x XP Booster (2h)", "cost": 250, "emoji": "⚡", "desc": "Double XP on chat, voice & focus for 2 hours"},
    {"id": "spinticket", "name": "🎟️ Lucky Spin Ticket", "cost": 100, "emoji": "🎟️", "desc": "Grants an extra free spin on the Lucky Wheel"},
    {"id": "vip", "name": "👑 VIP Prestige Role", "cost": 1000, "emoji": "👑", "desc": "Instant VIP Member role with golden name styling"},
    {"id": "coffee", "name": "☕ Coffee Break", "cost": 200, "emoji": "☕", "desc": "Redeem a coffee on the team"},
    {"id": "earlylog", "name": "🚀 Early Logout", "cost": 500, "emoji": "🚀", "desc": "Leave 1h early (manager approval needed)"},
    {"id": "wfh", "name": "🏠 WFH Half-day", "cost": 1000, "emoji": "🏠", "desc": "Convert a half-day into remote work"},
    {"id": "mvp", "name": "🏅 MVP Nomination", "cost": 800, "emoji": "🏅", "desc": "Nominate yourself or a peer for monthly MVP"},
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


_config_cache: Optional[dict] = None
_config_cache_ts: float = 0
CONFIG_CACHE_TTL = 30.0  # seconds to cache global configuration in memory


async def get_config() -> dict:
    global _config_cache, _config_cache_ts
    now = time.time()
    if _config_cache is not None and (now - _config_cache_ts) < CONFIG_CACHE_TTL:
        return _config_cache
    doc = await config_col.find_one({"_id": "global"})
    if doc is None:
        doc = {"_id": "global", "rewards": {}, "shop": SHOP_DEFAULT}
        await config_col.insert_one(doc)
    for k, v in [("rewards", {}), ("shop", SHOP_DEFAULT)]:
        if k not in doc:
            doc[k] = v
    _config_cache = doc
    _config_cache_ts = now
    return doc


async def save_config(doc: dict):
    global _config_cache, _config_cache_ts
    _config_cache = doc
    _config_cache_ts = time.time()
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
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_go())


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
                    announce_ch = bot.get_channel(SUMMARY_CHANNEL_ID)
                if not announce_ch:
                    for g in bot.guilds:
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
                        blog(f"birthday announce failed: {e}")
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
    if u.get("focus_minutes", 0) >= 100:
        give("deep-worker")
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
    try:
        await start_dashboard()
    except Exception as e:
        blog(f"dashboard failed to start (bot still runs): {e}")
    blog("✅ ready — commands live")


@bot.tree.error
async def _tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        cause = getattr(error, "original", error)
        cause_name = type(cause).__name__
        detail = str(cause)[:250] if str(cause) else cause_name
        blog(f"❌ /{(interaction.command.name if interaction.command else '?')}: {cause_name}: {detail}")
        msg = f"❌ Something broke: `{cause_name}` ({detail[:120]}). Check `docker compose logs bot` 📜"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


@bot.tree.command(name="ping", description="Health check — needs no database 🏓")
async def ping(interaction: discord.Interaction):
    ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! {ms}ms · bot is alive ✅")


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    # Starter bonus coins
    u = await get_user(member.id)
    u["coins"] += 50
    mark_dirty(member.id)
    track("events", "member_join")

    ch = member.guild.system_channel
    if not ch or not ch.permissions_for(member.guild.me).send_messages:
        for c in member.guild.text_channels:
            if any(k in c.name.lower() for k in ["welcome", "general", "lounge", "chat"]):
                if c.permissions_for(member.guild.me).send_messages:
                    ch = c
                    break
    if ch:
        embed = discord.Embed(
            title=f"🎉 Welcome to {member.guild.name}, {member.display_name}!",
            description=(
                f"Hey {member.mention}, welcome to the server! ✨\n\n"
                f"🎁 **Starter Gift:** We've credited **+50 🪙 coins** to your account!\n\n"
                f"**Quick Start:**\n"
                f"• `/checkin` — Check in daily to build your streak 🔥 & earn XP\n"
                f"• `/quests` — Complete daily quests for extra rewards 🗺️\n"
                f"• `/spin` — Spin the Lucky Wheel daily 🎡\n"
                f"• `/shop` — Spend your coins on perks, boosters & VIP 🏪\n"
                f"• `/birthday set` — Set your birthday for birthday bonuses 🎂\n"
                f"• `/dashboard` — Open your personal live web hub 📊"
            ),
            color=0x5865F2
        )
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Member #{member.guild.member_count} • Have a blast! 🚀")
        try:
            await ch.send(content=member.mention, embed=embed)
        except Exception as e:
            blog(f"Welcome message failed: {e}")


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
                ch_name = getattr(before.channel, "name", "").lower()
                is_focus_room = any(w in ch_name for w in ("focus", "study", "deep", "lounge"))
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
    used_freeze = False
    if u.get("last_checkin") == yesterday_str():
        u["streak"] = u.get("streak", 0) + 1
    else:
        # Check if user has a Streak Freeze in inventory
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


@bot.tree.command(name="daily", description="Claim daily coins (every 24h window by date)")
async def daily(interaction: discord.Interaction):
    await interaction.response.defer()
    u = await get_user(interaction.user.id)
    today = today_str()
    if u.get("last_daily") == today:
        await interaction.followup.send("⏳ Already claimed today. Come back tomorrow!",
                                                ephemeral=True)
        return
    u["last_daily"] = today
    reward = 100 + min(u.get("streak", 0), 30) * 5
    u["coins"] += reward
    mark_dirty(interaction.user.id)
    await interaction.followup.send(f"🎁 +{reward} coins! Balance: **{u['coins']}**")


@bot.tree.command(name="mystats", description="Show your (or another member's) gamified stats")
@app_commands.describe(member="Member to look up (default: you)")
async def mystats(interaction: discord.Interaction, member: Optional[discord.Member] = None):
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
    await interaction.response.defer()
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
    await interaction.followup.send(embed=discord.Embed(
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
    # Special perk handling: VIP Role
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


@bot.tree.command(name="shop", description="Browse and purchase rewards, perks & boosters")
async def shop(interaction: discord.Interaction):
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


@bot.tree.command(name="buy", description="Buy an item from the shop with coins")
@app_commands.describe(item_id="Item id from /shop (e.g. freeze, xp2x, spinticket, vip)")
async def buy(interaction: discord.Interaction, item_id: str):
    await interaction.response.defer()
    success, msg = await execute_purchase(interaction, item_id)
    if success:
        await interaction.followup.send(msg)
    else:
        await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="use", description="Use/activate an item from your inventory (e.g. xp2x, spinticket)")
@app_commands.describe(item_id="ID of the item in your inventory to consume")
async def use_item(interaction: discord.Interaction, item_id: str):
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


@bot.tree.command(name="shop_add", description="[Admin] Add or update a shop item")
@app_commands.describe(
    item_id="Unique identifier (e.g. pizza)",
    name="Item name (e.g. 🍕 Free Pizza)",
    cost="Cost in coins",
    desc="Item description"
)
async def shop_add(interaction: discord.Interaction, item_id: str, name: str, cost: int, desc: str):
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


@bot.tree.command(name="shop_remove", description="[Admin] Remove an item from the shop")
@app_commands.describe(item_id="ID of the item to remove")
async def shop_remove(interaction: discord.Interaction, item_id: str):
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



@bot.tree.command(name="inventory", description="Show your owned items")
async def inventory(interaction: discord.Interaction):
    await interaction.response.defer()
    u = await get_user(interaction.user.id)
    inv = u.get("inventory", [])
    desc = "\n".join(f"• **{it['name']}** (`{it['id']}`, {it.get('at', '?')})" for it in inv[-20:]) or "Empty. Visit /shop!"
    await interaction.followup.send(embed=discord.Embed(
        title=f"🎒 {interaction.user.display_name}'s Inventory", description=desc))


@bot.tree.command(name="badges", description="Show badges")
@app_commands.describe(member="Member (default: you)")
async def badges(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    await interaction.response.defer()
    member = member or interaction.user
    u = await get_user(member.id)
    await interaction.followup.send(embed=discord.Embed(
        title=f"🏅 {member.display_name}'s Badges",
        description=", ".join(f"`{b}`" for b in u.get("badges", [])) or "No badges yet.",
        color=0xFEE75C))


@bot.tree.command(name="attendance", description="Show attendance for last N days")
@app_commands.describe(member="Member (default: you)", days="Last N days (max 30)")
async def attendance(interaction: discord.Interaction,
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
        v = " 🎙️" if d in voice_days else ""
        lines.append(f"{c} {d}{v}")
    await interaction.followup.send(embed=discord.Embed(
        title=f"🗓️ {member.display_name} — {present}/{days} days",
        description="\n".join(lines), color=0x57F287).set_footer(
        text="✅ checkin · 🎙️ voice · 🌴 approved leave"))


# ---- leaves (with interactive modals & one-click approve/reject buttons) ----
class LeaveDecisionView(discord.ui.View):
    def __init__(self, leave_id: str, applicant_id: str, days: int = 1):
        super().__init__(timeout=None)
        self.leave_id = leave_id
        self.applicant_id = applicant_id
        self.days = days

    @discord.ui.button(label="Approve ✅", style=discord.ButtonStyle.success)
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Only managers can approve leaves.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            oid = ObjectId(self.leave_id)
        except Exception:
            await interaction.followup.send("❌ Bad leave ID.", ephemeral=True)
            return
        res = await leaves_col.update_one(
            {"_id": oid, "status": "pending"},
            {"$set": {"status": "approved", "decided_by": str(interaction.user.id),
                      "decided_at": datetime.now(timezone.utc).isoformat()}}
        )
        if not res.modified_count:
            await interaction.followup.send("⚠️ Leave was already decided or not found.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        orig_embed = interaction.message.embeds[0] if interaction.message.embeds else None
        new_embed = orig_embed.copy() if orig_embed else discord.Embed(title="📝 Leave Application")
        new_embed.color = 0x57F287
        new_embed.add_field(name="Decision", value=f"✅ **Approved** by {interaction.user.mention}", inline=False)
        await interaction.message.edit(embed=new_embed, view=self)
        await interaction.followup.send(f"✅ Approved leave for <@{self.applicant_id}>!", ephemeral=True)

        try:
            applicant = interaction.guild.get_member(int(self.applicant_id))
            if applicant:
                await applicant.send(f"🌴 Good news! Your leave request for **{self.days}d** has been **approved** by {interaction.user.display_name}! ✅")
        except Exception:
            pass

    @discord.ui.button(label="Reject ❌", style=discord.ButtonStyle.danger)
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ Only managers can reject leaves.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            oid = ObjectId(self.leave_id)
        except Exception:
            await interaction.followup.send("❌ Bad leave ID.", ephemeral=True)
            return
        res = await leaves_col.update_one(
            {"_id": oid, "status": "pending"},
            {"$set": {"status": "rejected", "decided_by": str(interaction.user.id),
                      "decided_at": datetime.now(timezone.utc).isoformat()}}
        )
        if not res.modified_count:
            await interaction.followup.send("⚠️ Leave was already decided or not found.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        orig_embed = interaction.message.embeds[0] if interaction.message.embeds else None
        new_embed = orig_embed.copy() if orig_embed else discord.Embed(title="📝 Leave Application")
        new_embed.color = 0xED4245
        new_embed.add_field(name="Decision", value=f"❌ **Rejected** by {interaction.user.mention}", inline=False)
        await interaction.message.edit(embed=new_embed, view=self)
        await interaction.followup.send(f"❌ Rejected leave for <@{self.applicant_id}>.", ephemeral=True)

        try:
            applicant = interaction.guild.get_member(int(self.applicant_id))
            if applicant:
                await applicant.send(f"⚠️ Your leave request for **{self.days}d** was **rejected** by {interaction.user.display_name}.")
        except Exception:
            pass


class LeaveApplyModal(discord.ui.Modal, title="📝 Apply for Leave"):
    from_date_input = discord.ui.TextInput(
        label="Start Date (YYYY-MM-DD)",
        placeholder="e.g. 2026-09-25 (leave empty for today)",
        required=False,
        max_length=10
    )
    days_input = discord.ui.TextInput(
        label="Number of Days (1-30)",
        placeholder="1",
        default="1",
        min_length=1,
        max_length=2
    )
    reason_input = discord.ui.TextInput(
        label="Reason for Leave",
        style=discord.TextStyle.paragraph,
        placeholder="Vacation, family event, medical, personal...",
        required=True,
        max_length=300
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            days = int(str(self.days_input.value).strip())
            days = max(1, min(days, 30))
        except ValueError:
            await interaction.followup.send("❌ Days must be a valid number.", ephemeral=True)
            return

        date_val = str(self.from_date_input.value).strip()
        try:
            d0 = datetime.strptime(date_val, "%Y-%m-%d").date() if date_val else datetime.now(timezone.utc).date()
        except ValueError:
            await interaction.followup.send("❌ Date must be in YYYY-MM-DD format.", ephemeral=True)
            return

        reason = str(self.reason_input.value).strip()
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


@bot.tree.command(name="leave_apply", description="Apply for leave (interactive modal form or direct args)")
@app_commands.describe(days="Number of days (opens popup form if omitted)", reason="Reason", from_date="Start YYYY-MM-DD")
async def leave_apply(interaction: discord.Interaction,
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


@bot.tree.command(name="my_leaves", description="Show your leave requests")
async def my_leaves(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = [d async for d in leaves_col.find(
        {"user_id": str(interaction.user.id)}).sort("created_at", -1).limit(10)]
    if not rows:
        await interaction.followup.send("No leaves yet.", ephemeral=True)
        return
    lines = [f"`{r['_id']}` {r['from']} ×{r['days']}d — **{r['status']}** — {r['reason']}" for r in rows]
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="leave_list", description="List pending leaves (managers)")
async def leave_list(interaction: discord.Interaction):
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


@bot.tree.command(name="leave_decide", description="Approve/reject a leave (managers)")
@app_commands.describe(leave_id="Leave id from /leave_list", approve="Approve?")
async def leave_decide(interaction: discord.Interaction, leave_id: str, approve: bool):
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


# ---- admin: rewards/economy ----
@bot.tree.command(name="reward_add", description="Map a level to a role (admin)")
@app_commands.describe(level="Level number", role="Role to grant")
async def reward_add(interaction: discord.Interaction, level: int, role: discord.Role):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cfg = await get_config()
    cfg.setdefault("rewards", {})[str(level)] = role.id
    await save_config(cfg)
    await interaction.followup.send(f"✅ Level {level} → {role.mention}", ephemeral=True)


@bot.tree.command(name="reward_list", description="Show level-role rewards")
async def reward_list(interaction: discord.Interaction):
    await interaction.response.defer()
    cfg = await get_config()
    rewards = cfg.get("rewards", {})
    if not rewards:
        await interaction.followup.send("No level rewards yet. Use /reward_add.", ephemeral=True)
        return
    lines = [f"Lv **{lvl}** → <@&{rid}>" for lvl, rid in sorted(rewards.items(), key=lambda x: int(x[0]))]
    await interaction.followup.send("\n".join(lines))


@bot.tree.command(name="give_coins", description="Give coins (admin)")
@app_commands.describe(member="Who", amount="How many")
async def give_coins(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ Manage Server needed.", ephemeral=True)
        return
    await interaction.response.defer()
    u = await get_user(member.id)
    u["coins"] += amount
    mark_dirty(member.id)
    await interaction.followup.send(f"🪙 Gave {amount} coins to {member.mention}. Balance: {u['coins']}")


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


@bot.tree.command(name="bounty_list", description="Show open bounties")
async def bounty_list(interaction: discord.Interaction):
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


@bot.tree.command(name="bounty_claim", description="Claim an open bounty task")
@app_commands.describe(bounty_id="Bounty id from /bounty_list")
async def bounty_claim(interaction: discord.Interaction, bounty_id: str):
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
    view = BountyDecisionView(bounty_id, poster_id, str(interaction.user.id), int(doc["coins"]), doc["title"])
    await interaction.followup.send(embed=embed, view=view)


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


# ---- deep work focus & lucky wheel gamification ----
_active_focus: dict = {}


class FocusControlView(discord.ui.View):
    def __init__(self, uid: str, task_name: str, minutes: int, start_ts: float, reward_xp: int, reward_coins: int):
        super().__init__(timeout=None)
        self.uid = uid
        self.task_name = task_name
        self.minutes = minutes
        self.start_ts = start_ts
        self.reward_xp = reward_xp
        self.reward_coins = reward_coins

    @discord.ui.button(label="End Focus Early ⏹️", style=discord.ButtonStyle.secondary)
    async def end_early(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("❌ This is not your focus session.", ephemeral=True)
            return
        await interaction.response.defer()
        session = _active_focus.pop(self.uid, None)
        if session and session.get("task_handle"):
            session["task_handle"].cancel()

        elapsed_min = max(1, int((time.time() - self.start_ts) // 60))
        for child in self.children:
            child.disabled = True

        if elapsed_min >= 5:
            frac = min(1.0, elapsed_min / self.minutes)
            earned_xp = max(5, int(self.reward_xp * frac))
            earned_coins = max(2, int(self.reward_coins * frac))
            u = await get_user(self.uid)
            u["xp"] += earned_xp
            u["coins"] += earned_coins
            u.setdefault("focus_minutes", 0)
            u["focus_minutes"] += elapsed_min
            bump_season(u, xp=earned_xp)
            grant_badges(u)
            mark_dirty(self.uid)
            msg = f"⏹️ **Focus ended early.** Focused for **{elapsed_min}m**.\nProrated rewards: +{earned_xp} XP · +{earned_coins} 🪙"
        else:
            msg = f"⏹️ **Focus cancelled** ({elapsed_min}m). Focus for at least 5m to earn rewards!"

        orig = interaction.message.embeds[0] if interaction.message.embeds else None
        new_emb = orig.copy() if orig else discord.Embed(title="🎯 Focus Session")
        new_emb.color = 0x747F8D
        new_emb.add_field(name="Session Ended", value=msg, inline=False)
        await interaction.message.edit(embed=new_emb, view=self)
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Check Status ⏱️", style=discord.ButtonStyle.primary)
    async def status_check(self, interaction: discord.Interaction, button: discord.ui.Button):
        elapsed_sec = int(time.time() - self.start_ts)
        rem_sec = max(0, (self.minutes * 60) - elapsed_sec)
        el_m, el_s = divmod(elapsed_sec, 60)
        re_m, re_s = divmod(rem_sec, 60)
        await interaction.response.send_message(
            f"⏱️ **Focus Status**: {el_m}m {el_s}s elapsed · **{re_m}m {re_s}s remaining** for *{self.task_name}*.",
            ephemeral=True
        )


@bot.tree.command(name="focus", description="Start a Pomodoro deep work session with rewards (5-120 min) 🎯")
@app_commands.describe(minutes="Duration in minutes (default 25)", task="What you are focusing on")
async def focus(interaction: discord.Interaction, minutes: int = 25, task: Optional[str] = "Deep Work"):
    await interaction.response.defer()
    uid = str(interaction.user.id)
    if uid in _active_focus:
        await interaction.followup.send(
            "⏳ You already have an active focus session! Use the button on your active card to end it early or check status.",
            ephemeral=True
        )
        return

    minutes = max(5, min(minutes, 120))
    task_name = (task or "Deep Work")[:80]
    reward_xp = max(10, int(minutes * 0.8))
    reward_coins = max(5, int(minutes * 0.5))
    now = time.time()
    end_ts = now + (minutes * 60)

    embed = discord.Embed(
        title="🎯 Deep Work Focus Session Started!",
        color=0x5865F2,
        description=f"**Member**: {interaction.user.mention}\n"
                    f"**Task**: **{task_name}**\n"
                    f"**Duration**: {minutes} minutes\n"
                    f"**Finishes**: <t:{int(end_ts)}:R> (<t:{int(end_ts)}:t>)\n\n"
                    f"🏆 **Completion Rewards**: +{reward_xp} XP · +{reward_coins} 🪙\n"
                    f"💡 *Mute distractions, open your editor, and lock in!*"
    )
    view = FocusControlView(uid, task_name, minutes, now, reward_xp, reward_coins)
    msg = await interaction.followup.send(embed=embed, view=view)

    async def _focus_timer():
        try:
            await asyncio.sleep(minutes * 60)
            if uid not in _active_focus:
                return
            _active_focus.pop(uid, None)
            u = await get_user(uid)
            u["xp"] += reward_xp
            u["coins"] += reward_coins
            u.setdefault("focus_minutes", 0)
            u["focus_minutes"] += minutes
            bump_season(u, xp=reward_xp)
            new_badges = grant_badges(u)
            mark_dirty(uid)
            track("commands", "focus_complete")

            try:
                for child in view.children:
                    child.disabled = True
                fin_emb = embed.copy()
                fin_emb.color = 0x57F287
                fin_emb.add_field(name="Status", value=f"✅ **Session Completed!** (+{reward_xp} XP, +{reward_coins} 🪙)", inline=False)
                await msg.edit(embed=fin_emb, view=view)
            except Exception:
                pass

            badge_text = f" 🏅 New badge: `{', '.join(new_badges)}`!" if new_badges else ""
            congrats = (f"🎉 **Focus Session Complete!** {interaction.user.mention} wrapped up **{minutes}m** of **{task_name}**! "
                        f"Awarded +{reward_xp} XP, +{reward_coins} 🪙.{badge_text} Take a 5-minute break! ☕")
            try:
                if interaction.channel:
                    await interaction.channel.send(congrats)
            except Exception:
                pass
        except asyncio.CancelledError:
            pass

    task_handle = asyncio.create_task(_focus_timer())
    _active_focus[uid] = {
        "task_name": task_name, "minutes": minutes, "start_ts": now,
        "reward_xp": reward_xp, "reward_coins": reward_coins,
        "task_handle": task_handle
    }


@bot.tree.command(name="spin", description="Spin the Daily Lucky Wheel for coins, XP, and badges! 🎰")
async def spin(interaction: discord.Interaction):
    await interaction.response.defer()
    uid = str(interaction.user.id)
    u = await get_user(uid)
    today = today_str()
    is_free = (u.get("last_spin") != today)
    cost = 0 if is_free else 25

    if not is_free and u.get("coins", 0) < cost:
        await interaction.followup.send(
            f"⏳ You already used your free spin today!\nExtra spins cost **{cost} 🪙** (your balance: {u.get('coins', 0)} 🪙).\nCome back tomorrow or earn coins from check-ins & quests!",
            ephemeral=True
        )
        return

    if not is_free:
        u["coins"] -= cost
    else:
        u["last_spin"] = today

    # Prizes: (label, coins, xp, badge, weight)
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
        badges = set(u.get("badges", []))
        if p_badge not in badges:
            badges.add(p_badge)
            new_badge = True
        u["badges"] = sorted(badges)

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
    poster = request["dash_user"].get("created_by") or request["dash_user"]["user_id"]
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
    await interaction.response.defer(ephemeral=True)
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
    await interaction.followup.send(
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


birthday_group = app_commands.Group(name="birthday", description="Birthday tracker & celebrations 🎂")


@birthday_group.command(name="set", description="Set your birthday to receive server celebrations & gifts!")
@app_commands.describe(month="Month of birth (1-12)", day="Day of birth (1-31)")
async def birthday_set(interaction: discord.Interaction, month: int, day: int):
    if not (1 <= month <= 12 and 1 <= day <= 31):
        await interaction.response.send_message("❌ Invalid date. Month must be 1–12 and Day 1–31.", ephemeral=True)
        return
    bday_str = f"{month:02d}-{day:02d}"
    u = await get_user(interaction.user.id)
    u["birthday"] = bday_str
    mark_dirty(interaction.user.id)
    track("commands", "birthday_set")
    await interaction.response.send_message(
        f"🎂 Your birthday has been set to **{bday_str}** (MM-DD)! We'll celebrate you when the day arrives! 🎉",
        ephemeral=True
    )


@birthday_group.command(name="list", description="List upcoming server birthdays")
async def birthday_list(interaction: discord.Interaction):
    await interaction.response.defer()
    docs = [doc async for doc in users_col.find({"birthday": {"$ne": None}})]
    if not docs:
        await interaction.followup.send("🎂 No birthdays registered yet! Set yours with `/birthday set`.")
        return
    now_mm_dd = datetime.now(timezone.utc).strftime("%m-%d")
    upcoming = sorted([u for u in docs if u.get("birthday") and u["birthday"] >= now_mm_dd], key=lambda x: x["birthday"])
    passed = sorted([u for u in docs if u.get("birthday") and u["birthday"] < now_mm_dd], key=lambda x: x["birthday"])
    ordered = upcoming + passed
    lines = [f"• **{doc.get('birthday')}** — <@{doc['_id']}>" for doc in ordered[:25]]
    embed = discord.Embed(
        title="🎂 Upcoming Server Birthdays",
        description="\n".join(lines) or "No birthdays found.",
        color=0xEB459E
    )
    embed.set_footer(text="Set your birthday anytime with /birthday set <month> <day>")
    await interaction.followup.send(embed=embed)


bot.tree.add_command(birthday_group)


strike_group = app_commands.Group(name="strike", description="Moderation strikes & warnings 🛡️")


@strike_group.command(name="add", description="[Mod] Issue a strike to a member (3 strikes = 1h auto-timeout)")
@app_commands.describe(member="Member to strike", reason="Reason for the strike")
async def strike_add(interaction: discord.Interaction, member: discord.Member, reason: str):
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
async def strike_list(interaction: discord.Interaction, member: discord.Member):
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
async def strike_clear(interaction: discord.Interaction, member: discord.Member):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("❌ Manage Messages permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    res = await strikes_col.delete_many({"user_id": str(member.id), "guild_id": str(interaction.guild.id)})
    await interaction.followup.send(f"✅ Cleared **{res.deleted_count}** strikes for {member.mention}!", ephemeral=True)


bot.tree.add_command(strike_group)


@bot.tree.command(name="slowmode", description="[Mod] Set text channel slowmode delay")
@app_commands.describe(seconds="Slowmode in seconds (0 to turn off, max 21600)", channel="Channel (default: current)")
async def slowmode(interaction: discord.Interaction, seconds: int, channel: Optional[discord.TextChannel] = None):
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


async def _schedule_reminder(delay_seconds: float, user_id: int, channel_id: int, note: str):
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    embed = discord.Embed(
        title="⏰ Reminder!",
        description=f"Hey <@{user_id}>, here is your scheduled reminder:\n\n> **{note}**",
        color=0xFEE75C
    )
    ch = bot.get_channel(channel_id)
    delivered = False
    if ch:
        try:
            await ch.send(content=f"<@{user_id}>", embed=embed)
            delivered = True
        except Exception:
            pass
    if not delivered:
        try:
            u = bot.get_user(user_id) or await bot.fetch_user(user_id)
            if u:
                await u.send(embed=embed)
        except Exception:
            pass


@bot.tree.command(name="remindme", description="Set a timer reminder (e.g. 10m, 60m)")
@app_commands.describe(minutes="Minutes until reminder (1 to 10080)", message="Reminder note")
async def remindme(interaction: discord.Interaction, minutes: int, message: str):
    if minutes < 1 or minutes > 10080:
        await interaction.response.send_message("❌ Minutes must be between 1 and 10080 (up to 7 days).", ephemeral=True)
        return
    track("commands", "remindme")
    remind_time = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    ts = int(remind_time.timestamp())
    asyncio.create_task(_schedule_reminder(minutes * 60, interaction.user.id, interaction.channel_id, message))
    await interaction.response.send_message(
        f"⏰ **Reminder set!** I'll remind you in **{minutes} minutes** (<t:{ts}:R>):\n> {message}"
    )


@bot.tree.command(name="focus_rooms", description="List voice lounges giving 1.5x Focus bonus XP")
async def focus_rooms(interaction: discord.Interaction):
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


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN not set.")
    bot.run(TOKEN)
