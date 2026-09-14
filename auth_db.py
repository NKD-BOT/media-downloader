"""
auth_db.py
Tracks which group/supergroup chats the bot owner has authorized for
/leech via the /a command -- a plain JSON file on disk, mirroring
settings_db.py's local-file pattern. No external database required.

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


def is_authorized(chat_id: int) -> bool:
    return chat_id in _load()


def toggle(chat_id: int) -> bool:
    """Flips this chat's authorization on/off. Returns the new state
    (True = now authorized, False = now de-authorized)."""
    chats = _load()
    if chat_id in chats:
        chats.discard(chat_id)
        _save(chats)
        return False
    chats.add(chat_id)
    _save(chats)
    return True
