"""Configuration and constants for Glyte Discord Bot."""
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

load_dotenv()

TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
MONGO_URI = (os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017").strip()
MONGO_DB = (os.getenv("MONGO_DB") or os.getenv("MONGO_DATABASE") or os.getenv("MONGODB_DATABASE") or "discord_bot").strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except (ValueError, TypeError):
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

# Privileged intents toggles
INTENTS_MEMBERS = (os.getenv("INTENTS_MEMBERS") or "false").lower() in ("true", "1", "yes")
INTENTS_MESSAGE_CONTENT = (os.getenv("INTENTS_MESSAGE_CONTENT") or "false").lower() in ("true", "1", "yes")

# Game Constants
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


def today_str(tz=timezone.utc):
    return datetime.now(tz).date().isoformat()


def yesterday_str(tz=timezone.utc):
    return (datetime.now(tz).date() - timedelta(days=1)).isoformat()


def season_str(dt=None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m")


def sched_tz():
    if ZoneInfo:
        try:
            return ZoneInfo(SUMMARY_TZ)
        except Exception:
            pass
    return timezone.utc
