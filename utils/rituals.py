"""Team rituals, insights compiler, quest auto-tuner, and EOD generator."""
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import discord

from config import (
    QUESTS, SUMMARY_CHANNEL_ID, STREAK_BONUS
)
from database import (
    users_col, leaves_col, stats_col, feedback_col, get_config, get_user, mark_dirty, flush_cache
)
from utils.helpers import (
    blog, fmt_duration, calc_level, xp_into_level, progress_bar
)


def quest_progress(u: dict, day: str, targets: dict = None) -> dict:
    d = u.get("daily", {}).get(day, {"messages": 0, "voice": 0})
    return {
        "checkin": 1 if day in u.get("checkins", []) else 0,
        "chatter": d.get("messages", 0),
        "voicer": d.get("voice", 0),
        "reactor": d.get("day_reactions", d.get("reactions", 0)),
    }



async def approved_leave_map() -> dict:
    today = datetime.now(timezone.utc).date()
    cur = leaves_col.find({"status": "approved"})
    user_days = defaultdict(set)
    async for lv in cur:
        try:
            d0 = datetime.fromisoformat(lv["from"]).date()
            days = int(lv.get("days", 1))
            for i in range(days):
                user_days[lv["user_id"]].add((d0 + timedelta(days=i)).isoformat())
        except Exception:
            pass
    return user_days


async def build_eod(guild: discord.Guild, day: str) -> discord.Embed:
    on_leave = await approved_leave_map()
    docs = [d async for d in users_col.find({})]
    present, onleave, absent = [], [], []
    members_by_id = {str(m.id): m for m in guild.members if not m.bot}

    for uid, m in members_by_id.items():
        u = next((d for d in docs if d["_id"] == uid), None)
        chk = (day in u.get("checkins", [])) if u else False
        vday = (day in u.get("voice_days", [])) if u else False
        lv = day in on_leave.get(uid, set())
        name = m.display_name

        if chk or vday:
            tag = "✅🎙️" if (chk and vday) else ("✅" if chk else "🎙️")
            st = u.get("streak", 0) if u else 0
            present.append(f"{tag} **{name}** (🔥{st})")
        elif lv:
            onleave.append(f"🌴 {name}")
        else:
            absent.append(name)

    embed = discord.Embed(
        title=f"🌙 End-of-Day Attendance — {day}",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name=f"✅ Present ({len(present)})",
                    value="\n".join(present) if present else "—", inline=False)
    if onleave:
        embed.add_field(name=f"🌴 On Leave ({len(onleave)})",
                        value="\n".join(onleave), inline=False)
    embed.add_field(name=f"❌ Absent ({len(absent)})",
                    value=", ".join(absent) if absent else "Everyone made it! 🎉", inline=False)
    tot = len(present) + len(onleave) + len(absent)
    pct = round(100 * len(present) / tot) if tot else 0
    embed.set_footer(text=f"Turnout: {pct}% · Glyte Attendance Tracker ✨")
    return embed


async def eff_targets() -> dict:
    cfg = await get_config()
    saved = cfg.get("quest_targets", {})
    return {qid: saved.get(qid, q["target"]) for qid, q in QUESTS.items()}


async def tune_quests(cfg: dict) -> list:
    end = datetime.now(timezone.utc).date()
    dates = [(end - timedelta(days=i)).isoformat() for i in range(7)]
    users = await users_col.count_documents({})
    if users == 0:
        return []
    claims = {}
    for qid in QUESTS:
        claims[qid] = await users_col.count_documents(
            {f"quest_claimed.{qid}": {"$in": dates}})
    actions = []
    targets = cfg.get("quest_targets", {qid: q["target"] for qid, q in QUESTS.items()})
    for qid, q in QUESTS.items():
        rate = claims[qid] / (users * 7)
        old = targets.get(qid, q["target"])
        new = old
        if rate < 0.20 and old > max(1, int(q["target"] * 0.5)):
            new = max(1, int(old * 0.9))
        elif rate > 0.80 and old < int(q["target"] * 2.0):
            new = max(old + 1, int(old * 1.1))
        if new != old:
            targets[qid] = new
            actions.append({"quest": qid, "old": old, "new": new,
                            "rate": round(rate, 2), "at": datetime.now(timezone.utc).isoformat()})
    cfg["quest_targets"] = targets
    log = cfg.setdefault("tune_log", [])
    log.extend(actions)
    cfg["tune_log"] = log[-50:]
    return actions


async def build_insights() -> dict:
    cfg = await get_config()
    end = datetime.now(timezone.utc).date()
    d7 = [(end - timedelta(days=i)).isoformat() for i in range(7)]
    u_count = await users_col.count_documents({})
    chk_7 = await users_col.count_documents({"checkins": {"$in": d7}})
    kd_docs = [d async for d in stats_col.find({"_id": {"$in": d7}})]
    kudos_7 = sum(d.get("kudos", 0) for d in kd_docs)
    targets = await eff_targets()
    q_data = {}
    for qid, q in QUESTS.items():
        c = await users_col.count_documents({f"quest_claimed.{qid}": {"$in": d7}})
        rate = round(c / max(1, u_count * 7), 2)
        q_data[qid] = {"name": q["name"], "target": targets[qid], "rate": rate, "claims_7": c}
    sugg = []
    if u_count and (chk_7 / u_count) < 2.0:
        sugg.append("⚠️ Low check-in turnout this week — consider a friendly nudge or streak challenge.")
    if kudos_7 == 0:
        sugg.append("💡 Zero kudos given in 7 days — remind team with /kudos.")
    for qid, qd in q_data.items():
        if qd["rate"] < 0.15:
            sugg.append(f"🎯 Quest **{qd['name']}** completion is low ({int(qd['rate']*100)}%) — auto-tuner will lower the target.")
        elif qd["rate"] > 0.85:
            sugg.append(f"🔥 Quest **{qd['name']}** is easily cleared ({int(qd['rate']*100)}%) — target nudging up.")
    fb = [f async for f in feedback_col.find({}).sort("at", -1).limit(5)]
    if fb:
        sugg.append(f"💌 {len(fb)} recent feedback note(s) in inbox.")
    return {"users": u_count, "checkins_7": chk_7, "kudos_7": kudos_7,
            "quests": q_data, "tune_log": cfg.get("tune_log", [])[-5:],
            "feedback": [{"by": f["user_id"], "text": f["text"][:200]} for f in fb],
            "suggestions": sugg}


async def award_season_champion(prev: str, bot: discord.Client):
    cur = users_col.find({f"seasons.{prev}.xp": {"$gt": 0}})
    best, best_xp = None, 0
    async for d in cur:
        x = d.get("seasons", {}).get(prev, {}).get("xp", 0)
        if x > best_xp:
            best, best_xp = d, x
    if best:
        u = await get_user(best["_id"])
        u["coins"] += 1000
        u.setdefault("badges", []).append(f"champion-{prev}")
        u["badges"] = sorted(set(u["badges"]))
        mark_dirty(best["_id"])
        await flush_cache()
        if SUMMARY_CHANNEL_ID:
            ch = bot.get_channel(SUMMARY_CHANNEL_ID)
            if ch:
                try:
                    await ch.send(f"👑 **Season {prev} champion**: <@{best['_id']}> with **{best_xp} XP**! +1000 🪙 + exclusive badge 🏅")
                except Exception as e:
                    blog(f"champion announce failed: {e}")
