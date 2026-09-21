"""Database layer: MongoDB Atlas client, in-memory cache, and background flush."""
import asyncio
import time
from typing import Optional, Union
import motor.motor_asyncio
from pymongo import ReplaceOne

from config import (
    MONGO_URI, MONGO_DB, DEFAULT_USER, SHOP_DEFAULT, today_str
)

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

mongo = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, **mongo_kwargs)
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

# In-memory Speed Layer
_cache: dict = {}
_dirty: set = set()
_config_cache: Optional[dict] = None
_config_cache_ts: float = 0
CONFIG_CACHE_TTL = 30.0


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
            await users_col.replace_one({"_id": uid}, doc)
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
        loop.create_task(_go())
    except RuntimeError:
        pass
