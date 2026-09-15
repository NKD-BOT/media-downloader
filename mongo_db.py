"""
mongo_db.py
Optional MongoDB-backed storage for per-user /usetting preferences and
leech usage stats. Used automatically whenever DB_URI is configured;
settings_db.py otherwise transparently falls back to its built-in JSON
file store, so adding MongoDB is a drop-in upgrade, not a requirement.

Uses `motor` (the async MongoDB driver) so nothing here ever blocks the
bot's event loop. If DB_URI is set but `motor` isn't installed, or the
connection fails, every function here degrades to a no-op and logs a
warning -- the bot keeps working on the JSON fallback instead of crashing.
"""

import datetime
import logging
from typing import Any, Dict, Optional

import config

logger = logging.getLogger("leech.mongo_db")

_client = None
_db = None
_warned = False


def mongo_enabled() -> bool:
    return bool(config.DB_URI)


def _get_db():
    """Lazily connects on first use and caches the connection. Returns
    None (and logs a warning, once) if MongoDB isn't usable for any
    reason -- callers treat that as "fall back to the JSON store"."""
    global _client, _db, _warned

    if _db is not None:
        return _db
    if not mongo_enabled():
        return None

    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        if not _warned:
            logger.warning(
                "DB_URI is set but the 'motor' package isn't installed -- "
                "falling back to the local JSON settings store. Add "
                "'motor' to requirements.txt to enable MongoDB."
            )
            _warned = True
        return None

    try:
        _client = AsyncIOMotorClient(config.DB_URI, serverSelectionTimeoutMS=5000)
        _db = _client[config.DB_NAME]
    except Exception as exc:  # noqa: BLE001
        if not _warned:
            logger.warning("Could not connect to MongoDB (%s) -- falling back to the JSON store.", exc)
            _warned = True
        _db = None

    return _db


async def get_settings(user_id: int) -> Optional[Dict[str, Any]]:
    db = _get_db()
    if db is None:
        return None
    try:
        return await db.user_settings.find_one({"_id": user_id})
    except Exception as exc:  # noqa: BLE001
        logger.warning("MongoDB read failed for user %s: %s", user_id, exc)
        return None


async def save_settings(user_id: int, settings: Dict[str, Any]) -> bool:
    db = _get_db()
    if db is None:
        return False
    try:
        await db.user_settings.update_one({"_id": user_id}, {"$set": settings}, upsert=True)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("MongoDB write failed for user %s: %s", user_id, exc)
        return False


async def record_leech(user_id: int, username: Optional[str], bytes_amount: int) -> None:
    """Called once per completed /leech -- tracks per-user usage stats
    (file count, total bytes, first/last-seen) for the /stats command.
    A pure no-op if MongoDB isn't configured/available."""
    db = _get_db()
    if db is None:
        return
    try:
        await db.user_stats.update_one(
            {"_id": user_id},
            {
                "$set": {"username": username},
                "$setOnInsert": {"first_seen": datetime.datetime.utcnow()},
                "$inc": {"files_leeched": 1, "bytes_leeched": max(bytes_amount, 0)},
                "$currentDate": {"last_used": True},
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("MongoDB stats update failed for user %s: %s", user_id, exc)


async def get_stats(user_id: int) -> Optional[Dict[str, Any]]:
    db = _get_db()
    if db is None:
        return None
    try:
        return await db.user_stats.find_one({"_id": user_id})
    except Exception as exc:  # noqa: BLE001
        logger.warning("MongoDB stats read failed for user %s: %s", user_id, exc)
        return None


async def is_chat_authorized(chat_id: int) -> Optional[bool]:
    """Returns whether chat_id is an owner-authorized group for /leech,
    or None (not True/False) if MongoDB isn't available -- callers should
    fall back to their local JSON store in that case, not treat None as
    "not authorized"."""
    db = _get_db()
    if db is None:
        return None
    try:
        doc = await db.authorized_chats.find_one({"_id": chat_id})
        return doc is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("MongoDB authorized-chat read failed for %s: %s", chat_id, exc)
        return None


async def toggle_chat_authorization(chat_id: int) -> Optional[bool]:
    """Flips chat_id's authorization on/off in MongoDB. Returns the new
    state, or None if MongoDB isn't available (caller should fall back to
    the local JSON store)."""
    db = _get_db()
    if db is None:
        return None
    try:
        existing = await db.authorized_chats.find_one({"_id": chat_id})
        if existing:
            await db.authorized_chats.delete_one({"_id": chat_id})
            return False
        await db.authorized_chats.insert_one({"_id": chat_id})
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("MongoDB authorized-chat toggle failed for %s: %s", chat_id, exc)
        return None
