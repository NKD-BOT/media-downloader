"""
telegraph.py
Minimal Telegraph (graph.org) API client -- just enough to publish a
MediaInfo report as a page and get back a shareable link, matching how
most MediaInfo Telegram bots present their results. Uses a single
anonymous account created on first use (kept in memory for the process's
lifetime); if Telegraph is unreachable or rejects the request, callers
get None back and should fall back to sending the report directly.
"""

import logging
from typing import Optional

import aiohttp

logger = logging.getLogger("leech.telegraph")

_API_BASE = "https://api.telegra.ph"
_access_token: Optional[str] = None


async def _ensure_account() -> Optional[str]:
    global _access_token
    if _access_token:
        return _access_token

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API_BASE}/createAccount",
                data={"short_name": "LeechBot", "author_name": "Leech Bot"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegraph account creation failed: %s", exc)
        return None

    if isinstance(data, dict) and data.get("error"):
        logger.warning("Telegraph account creation error: %s", data["error"])
        return None

    token = (data or {}).get("access_token") or ((data or {}).get("result") or {}).get("access_token")
    if not token:
        logger.warning("Telegraph account creation returned no access_token: %s", data)
        return None

    _access_token = token
    return token


async def create_page(title: str, text: str) -> Optional[str]:
    """Publishes `text` as a single preformatted block on a new Telegraph
    page titled `title`, returning its URL -- or None if Telegraph is
    unreachable, the account couldn't be created, or the request fails."""
    token = await _ensure_account()
    if not token:
        return None

    content = [{"tag": "pre", "children": [text]}]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API_BASE}/createPage",
                json={
                    "access_token": token,
                    "title": (title or "MediaInfo")[:250],
                    "content": content,
                    "author_name": "Leech Bot",
                    "return_content": False,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegraph page creation failed: %s", exc)
        return None

    if isinstance(data, dict) and data.get("error"):
        logger.warning("Telegraph page creation error: %s", data["error"])
        return None

    result = (data or {}).get("result") or {}
    return result.get("url")
