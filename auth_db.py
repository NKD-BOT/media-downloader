"""
auth_db.py
Tracks which group/supergroup chats the bot owner has authorized for
/leech via the /a command. Uses MongoDB (via mongo_db.py) automatically
whenever DB_URI is configured -- important because a plain JSON file on
disk gets wiped on every redeploy on ephemeral hosts (Railway, etc.),
silently de-authorizing every group each time the bot is updated.
Falls back to a local JSON file when MongoDB isn't configured/available.

Access model (enforced in handlers.py, not here):
- The bot's private chat (PM) only works for /leech when the sender is
  the configured OWNER_ID.
- A group/supergroup only works for /leech once the owner has run /a in
  it; anyone else (owner included) gets a clear "not authorized" message
  until then.
"""

import json
import logging
import os
from typing import Set

import mongo_db

logger = logging.getLogger("leech.auth_db")

_DB_PATH = os.getenv("AUTHORIZED_CHATS_PATH", "authorized_chats.json")


def _load() -> Set[int]:
    if not os.path.exists(_DB_PATH):
        return set()
    try:
        with open(_DB_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s (%s); starting with no authorized chats.", _DB_PATH, exc)
        return set()


def _save(chat_ids: Set[int]) -> None:
    tmp_path = _DB_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(sorted(chat_ids), f)
    os.replace(tmp_path, _DB_PATH)  # atomic on POSIX filesystems


async def is_authorized(chat_id: int) -> bool:
    if mongo_db.mongo_enabled():
        result = await mongo_db.is_chat_authorized(chat_id)
        if result is not None:
            return result
        # Mongo configured but unreachable right now -- fall through to
        # the JSON store rather than treating every group as unauthorized.
    return chat_id in _load()


async def toggle(chat_id: int) -> bool:
    """Flips this chat's authorization on/off. Returns the new state
    (True = now authorized, False = now de-authorized)."""
    if mongo_db.mongo_enabled():
        result = await mongo_db.toggle_chat_authorization(chat_id)
        if result is not None:
            return result

    chats = _load()
    if chat_id in chats:
        chats.discard(chat_id)
        _save(chats)
        return False
    chats.add(chat_id)
    _save(chats)
    return True
