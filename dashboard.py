"""Web Dashboard and JSON API endpoints for Glyte Bot."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from aiohttp import web
from bson import ObjectId
import discord

from config import (
    BASE_DIR, DASHBOARD_PORT, SUMMARY_CHANNEL_ID, STANDUP_CHANNEL_ID,
    SHOP_DEFAULT, QUESTS, today_str, season_str
)
from database import (
    flush_cache, get_config, save_config, get_user, mark_dirty, track,
    users_col, leaves_col, bounties_col, standups_col, dash_tokens_col, dash_access_col
)
from utils.helpers import (
    blog, _bot_logs, xp_into_level, bump_season
)
from utils.rituals import (
    approved_leave_map, build_eod, build_insights, eff_targets, quest_progress
)

_dash_runner = None
_bot_instance = None


def _dash_guild():
    global _bot_instance
    if not _bot_instance:
        return None
    for cid in (SUMMARY_CHANNEL_ID, STANDUP_CHANNEL_ID):
        if cid:
            ch = _bot_instance.get_channel(cid)
            if ch and getattr(ch, "guild", None):
                return ch.guild
    return _bot_instance.guilds[0] if _bot_instance.guilds else None


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
                {"ok": False, "error": "Bad or expired link — run /dashboard in Discord for a fresh link 🔄"},
                status=401
            )
        request["dash_user"] = doc
        return await handler(request)
    return wrapper


def _need_admin(handler):
    async def wrapper(request):
        doc = await _dash_auth(request)
        if not doc:
            return web.json_response(
                {"ok": False, "error": "Bad or expired link — run /dashboard in Discord for a fresh link 🔄"},
                status=401
            )
        if not doc.get("is_admin"):
            return web.json_response(
                {"ok": False, "error": "Admin access required 👑"},
                status=403
            )
        request["dash_user"] = doc
        return await handler(request)
    return wrapper


async def dash_page(request):
    doc = await _dash_auth(request)
    if not doc:
        return web.Response(
            text="🔒 Bad or expired link — run /dashboard in Discord for a fresh one ✨",
            status=401
        )
    try:
        await dash_access_col.insert_one({
            "user_id": doc["user_id"],
            "name": doc.get("user_name", "?"),
            "at": datetime.now(timezone.utc).isoformat(),
            "ip": request.remote
        })
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
    voice_only = sum(1 for x in docs if today in x.get("voice_days", []) and today not in x.get("checkins", []))
    pending = await leaves_col.count_documents({"status": "pending"})
    coins = sum(x.get("coins", 0) for x in docs)
    return web.json_response({
        "ok": True, "today": today, "members": len(docs),
        "present": present, "voice_only": voice_only,
        "pending_leaves": pending, "coins": coins, "series": series
    })


@_need_auth
async def api_leaderboard(request):
    key = request.query.get("key", "xp")
    limit = max(1, min(int(request.query.get("limit", "10") or 10), 50))
    names = _dash_names()
    if key == "checkins":
        cur = users_col.aggregate([
            {"$project": {"score": {"$size": {"$ifNull": ["$checkins", []]}}}},
            {"$sort": {"score": -1}}, {"$limit": limit}
        ])
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
            rows.append({
                "name": names.get(d["_id"], d["_id"]), "xp": s.get("xp", 0),
                "messages": s.get("messages", 0), "voice_min": round(s.get("voice", 0) / 60)
            })
    rows.sort(key=lambda r: r["xp"], reverse=True)
    return web.json_response({"ok": True, "month": month, "rows": rows[:15]})


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
        rows.append({
            "id": uid, "name": names.get(uid, uid),
            "present": len(set(checks) | set(voice)),
            "checkins": len(checks), "voice_days": len(voice),
            "leaves": len(date_set & on_leave.get(uid, set())),
            "streak": d.get("streak", 0)
        })
    rows.sort(key=lambda r: r["present"], reverse=True)
    return web.json_response({"ok": True, "days": days, "dates": dates, "rows": rows})


@_need_auth
async def api_members(request):
    names = _dash_names()
    return web.json_response({
        "ok": True,
        "rows": [{"id": k, "name": v} for k, v in sorted(names.items(), key=lambda x: x[1].lower())]
    })


@_need_auth
async def api_leaves(request):
    status = request.query.get("status", "pending")
    q = {} if status == "all" else {"status": status}
    names = _dash_names()
    rows = [d async for d in leaves_col.find(q).sort("created_at", -1).limit(50)]
    out = [{
        "id": str(r["_id"]), "name": names.get(str(r["user_id"]), str(r["user_id"])),
        "from": r.get("from", r.get("start_date", "")), "days": r.get("days", 1),
        "reason": r.get("reason", ""), "status": r.get("status", "")
    } for r in rows]
    return web.json_response({"ok": True, "rows": out})


@_need_admin
async def api_leave_decide(request):
    body = await request.json()
    try:
        oid = ObjectId(str(body.get("id", "")))
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid leave ID"}, status=400)
    status = "approved" if body.get("approve") else "rejected"
    res = await leaves_col.update_one({"_id": oid, "status": "pending"}, {"$set": {"status": status}})
    if not res.modified_count:
        return web.json_response({"ok": False, "error": "Leave request not found or already decided."})
    return web.json_response({"ok": True, "status": status})


@_need_auth
async def api_shop(request):
    cfg = await get_config()
    return web.json_response({"ok": True, "rows": cfg.get("shop", SHOP_DEFAULT)})


@_need_admin
async def api_shop_add(request):
    body = await request.json()
    item = {
        "id": str(body.get("id", "")).lower().strip(),
        "name": body.get("name", ""),
        "cost": int(body.get("cost", 0) or 0),
        "desc": body.get("description", "")
    }
    if not item["id"] or not item["name"] or item["cost"] <= 0:
        return web.json_response({"ok": False, "error": "ID, name and positive cost required"})
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
    out = [{
        "id": str(r["_id"]), "title": r["title"], "coins": r.get("coins", 0),
        "description": r.get("description", ""), "status": r.get("status", ""),
        "claimer": names.get(str(r.get("claimer_id") or ""), "")
    } for r in rows]
    return web.json_response({"ok": True, "rows": out})


@_need_admin
async def api_bounty_post(request):
    body = await request.json()
    coins = int(body.get("coins", 0) or 0)
    if not body.get("title") or coins <= 0:
        return web.json_response({"ok": False, "error": "Title and positive coin bounty required"})
    poster = request["dash_user"].get("created_by") or request["dash_user"]["user_id"]
    u = await get_user(poster)
    if u["coins"] < coins:
        return web.json_response({"ok": False, "error": f"Insufficient funds: only {u['coins']} 🪙 available"})
    u["coins"] -= coins
    mark_dirty(poster)
    res = await bounties_col.insert_one({
        "title": body["title"], "description": body.get("description", ""), "coins": coins,
        "poster_id": poster, "status": "open", "claimer_id": None,
        "created_at": datetime.now(timezone.utc).isoformat()
    })
    return web.json_response({"ok": True, "id": str(res.inserted_id)})


@_need_admin
async def api_bounty_approve(request):
    body = await request.json()
    try:
        oid = ObjectId(str(body.get("id", "")))
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid bounty ID"}, status=400)
    doc = await bounties_col.find_one({"_id": oid})
    if not doc or doc.get("status") != "claimed" or not doc.get("claimer_id"):
        return web.json_response({"ok": False, "error": "No claim awaiting approval"})
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
        return web.json_response({"ok": False, "error": "Member and non-zero amount needed"})
    u = await get_user(body["user_id"])
    u["coins"] += amount
    mark_dirty(body["user_id"])
    return web.json_response({"ok": True, "balance": u["coins"]})


@_need_admin
async def api_eod(request):
    g = _dash_guild()
    if not g:
        return web.json_response({"ok": False, "error": "No server found"})
    target = None
    if SUMMARY_CHANNEL_ID and _bot_instance:
        target = _bot_instance.get_channel(SUMMARY_CHANNEL_ID)
    if target is None:
        return web.json_response({"ok": False, "error": "Configure SUMMARY_CHANNEL_ID first"})
    await target.send(embed=await build_eod(g, today_str()))
    return web.json_response({"ok": True})


@_need_auth
async def api_standups(request):
    day = request.query.get("date") or today_str()
    names = _dash_names()
    rows = [d async for d in standups_col.find({"date": day}).sort("at", 1)]
    out = [{
        "name": names.get(str(r["user_id"]), str(r["user_id"])),
        "yesterday": r.get("yesterday", "—"), "today": r.get("today", "—"),
        "blockers": r.get("blockers", "—")
    } for r in rows]
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
    quests = [{
        "id": qid, "name": q["name"], "desc": q["desc"], "target": targets[qid],
        "progress": min(prog.get(qid, 0), targets[qid]),
        "done": prog.get(qid, 0) >= targets[qid],
        "claimed": claimed.get(qid) == day,
        "xp": q["xp"], "coins": q["coins"]
    } for qid, q in QUESTS.items()]
    names = _dash_names()
    return web.json_response({
        "ok": True, "id": uid, "name": names.get(uid, doc.get("user_name", "You")),
        "is_admin": bool(doc.get("is_admin")), "level": lvl, "xp": u["xp"],
        "into": into, "need": need, "coins": u["coins"], "streak": u["streak"],
        "messages": u["messages"], "voice_seconds": u["voice_seconds"],
        "checkins": len(u.get("checkins", [])), "badges": u.get("badges", []),
        "kudos": u.get("kudos_received", 0),
        "season_xp": u.get("seasons", {}).get(season_str(), {}).get("xp", 0),
        "quests": quests
    })


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
        return web.json_response({"ok": False, "error": "Unknown quest ID"})
    uid = request["dash_user"]["user_id"]
    u = await get_user(uid)
    day = today_str()
    if u.get("quest_claimed", {}).get(qid) == day:
        return web.json_response({"ok": False, "error": "Quest already claimed today"})
    targets = await eff_targets()
    if quest_progress(u, day).get(qid, 0) < targets[qid]:
        return web.json_response({"ok": False, "error": "Quest requirements not met yet"})
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
        return web.json_response({"ok": False, "error": "Unknown shop item"})
    uid = request["dash_user"]["user_id"]
    u = await get_user(uid)
    if u["coins"] < item["cost"]:
        return web.json_response({"ok": False, "error": f"Need {item['cost']} 🪙 (you have {u['coins']})"})
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
        {"id": str(r["_id"]), "from": r.get("from", r.get("start_date", "")), "days": r.get("days", 1),
         "reason": r.get("reason", ""), "status": r.get("status", "")} for r in rows
    ]})


@_need_auth
async def api_leave_apply_self(request):
    body = await request.json()
    days = max(1, min(int(body.get("days", 1) or 1), 30))
    try:
        d0 = datetime.strptime(body.get("from") or today_str(), "%Y-%m-%d").date()
    except ValueError:
        return web.json_response({"ok": False, "error": "Start date must be YYYY-MM-DD"})
    uid = request["dash_user"]["user_id"]
    res = await leaves_col.insert_one({
        "user_id": uid, "days": days, "reason": str(body.get("reason", ""))[:300],
        "from": d0.isoformat(), "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat()
    })
    return web.json_response({"ok": True, "id": str(res.inserted_id)})


@_need_admin
async def api_bot_logs(request):
    n = max(1, min(int(request.query.get("n", "150") or 150), 300))
    return web.json_response({"ok": True, "rows": list(_bot_logs)[-n:]})


@_need_admin
async def api_access_log(request):
    rows = [d async for d in dash_access_col.find({}).sort("at", -1).limit(100)]
    names = _dash_names()
    out = [{
        "name": names.get(str(r.get("user_id")), r.get("name", "?")),
        "at": r.get("at", ""), "ip": r.get("ip", "")
    } for r in rows]
    return web.json_response({"ok": True, "rows": out})


@_need_admin
async def api_insights(request):
    ins = await build_insights()
    names = _dash_names()
    for f in ins["feedback"]:
        f["by"] = names.get(str(f["by"]), str(f["by"]))
    return web.json_response({"ok": True, **ins})



async def landing_page(request):
    path = BASE_DIR / "landing.html"
    if not path.exists():
        return web.Response(text="landing.html missing", status=500)
    return web.FileResponse(path)


async def docs_page(request):
    path = BASE_DIR / "docs.html"
    if not path.exists():
        return web.Response(text="docs.html missing", status=500)
    return web.FileResponse(path)


async def robots_page(request):
    path = BASE_DIR / "robots.txt"
    if not path.exists():
        return web.Response(text="robots.txt missing", status=500)
    return web.FileResponse(path)


async def sitemap_page(request):
    path = BASE_DIR / "sitemap.xml"
    if not path.exists():
        return web.Response(text="sitemap.xml missing", status=500)
    return web.FileResponse(path)

async def start_dashboard(bot: discord.Client):
    global _dash_runner, _bot_instance
    _bot_instance = bot
    if _dash_runner is not None:
        return
    app = web.Application()
    app.router.add_get("/dash/", dash_page)
    app.router.add_get("/", landing_page)
    app.router.add_get("/docs", docs_page)
    app.router.add_get("/robots.txt", robots_page)
    app.router.add_get("/sitemap.xml", sitemap_page)
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
