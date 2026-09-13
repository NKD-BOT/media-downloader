"""
settings_db.py
Per-user "Leech Settings" storage. Uses MongoDB (via mongo_db.py)
automatically whenever DB_URI is configured; otherwise falls back to a
single local JSON file -- no external database service required either
way. Every other module (handlers.py, leech_settings.py) only ever calls
the functions here, so the storage backend is an implementation detail
they don't need to know about.

Settings stored here power the /usetting menu: filename prefix/suffix,
a caption template, a custom thumbnail, native-media vs. document
uploads, an embedded metadata title, an extra "dump" chat every file is
copied to, and filename find/replace ("name swap") pairs.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Tuple

import mongo_db

logger = logging.getLogger("leech.settings_db")

_DB_PATH = os.getenv("SETTINGS_DB_PATH", "user_settings.json")
_lock = asyncio.Lock()

DEFAULTS: Dict[str, Any] = {
    "send_as_document": False,   # False -> try native video/audio/photo upload
    "thumbnail_path": None,      # local path to a cached custom thumbnail
    "leech_prefix": "",
    "leech_suffix": "",
    "leech_caption": "",         # supports {filename} and {size}; empty -> just filename
    "metadata_title": None,      # embedded into video/audio via ffmpeg if set
    "dump_chat_id": None,        # every leeched file is also copied here if set
    "name_swap_enabled": False,
    "name_swap_pairs": [],       # list of [find, replace] applied to filenames
}


def _load_all() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(_DB_PATH):
        return {}
    try:
        with open(_DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s (%s); starting with empty settings.", _DB_PATH, exc)
        return {}


def _save_all(data: Dict[str, Dict[str, Any]]) -> None:
    tmp_path = _DB_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, _DB_PATH)  # atomic on POSIX filesystems


async def get_settings(user_id: int) -> Dict[str, Any]:
    """Returns this user's settings merged over the defaults (so newly
    added setting keys always have a sane value even for old records)."""
    if mongo_db.mongo_enabled():
        doc = await mongo_db.get_settings(user_id)
        if doc is not None:
            doc.pop("_id", None)
            return {**DEFAULTS, **doc}
        return dict(DEFAULTS)

    async with _lock:
        data = _load_all()
        return {**DEFAULTS, **data.get(str(user_id), {})}


async def update_settings(user_id: int, **changes: Any) -> Dict[str, Any]:
    if mongo_db.mongo_enabled():
        current = await get_settings(user_id)
        current.update(changes)
        saved = await mongo_db.save_settings(user_id, current)
        if saved:
            return current
        # Mongo write failed (e.g. connection drop) -- don't silently lose
        # the change, fall through to the JSON store as a safety net.

    async with _lock:
        data = _load_all()
        key = str(user_id)
        current = {**DEFAULTS, **data.get(key, {})}
        current.update(changes)
        data[key] = current
        _save_all(data)
        return current


async def add_name_swap_pair(user_id: int, find: str, replace: str) -> Dict[str, Any]:
    return await add_name_swap_pairs(user_id, [(find, replace)])


async def add_name_swap_pairs(user_id: int, pairs: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Adds several name-swap pairs in a single read-modify-write round
    trip -- used by the /usetting bulk-paste flow so adding 50 site-name
    filters doesn't mean 50 separate DB writes."""
    current = await get_settings(user_id)
    existing: List[List[str]] = list(current.get("name_swap_pairs", []))
    existing.extend([list(p) for p in pairs])
    return await update_settings(user_id, name_swap_pairs=existing)


async def clear_name_swap_pairs(user_id: int) -> Dict[str, Any]:
    return await update_settings(user_id, name_swap_pairs=[])


def get_name_swap_pairs(settings: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [(p[0], p[1]) for p in settings.get("name_swap_pairs", []) if len(p) == 2]
