"""
main.py
Entry point for the direct-link leech bot. Validates config and runs
the Pyrofork client.
"""

import asyncio
import json
import logging
import os
import sys

import aiohttp
from pyrogram import Client, idle

import config
from handlers import register_handlers
from leech_settings import register_settings_handlers
from torrent import start_aria2_daemon, stop_aria2_daemon

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("leech.main")


def build_client() -> Client:
    return Client(
        name="leech_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        # Keeps the MTProto session in memory instead of writing a
        # .session file to disk. Rules out disk-permission/corrupted-
        # session issues on hosts with a restrictive filesystem (e.g. a
        # container that resets on every deploy) -- there's nothing to
        # persist here anyway since this process is stateless between
        # restarts.
        in_memory=True,
        # Pyrofork sends/receives a file's parts sequentially over ONE
        # connection by default (max 1). Raising this lets it use several
        # connections at once for both uploads and downloads-to-the-bot
        # (e.g. reading an uploaded .torrent/thumbnail), often multiplying
        # real-world Telegram transfer speed.
        max_concurrent_transmissions=config.MAX_CONCURRENT_TRANSMISSIONS,
    )


async def _clear_webhook() -> None:
    """If a webhook was EVER set for this bot token (even by a totally
    different project, at any point in the past), Telegram delivers every
    update to that URL instead of to this MTProto session -- the bot
    connects and looks perfectly healthy, but literally never receives a
    single update. Bot-API's deleteWebhook is the only fix, and it's safe
    to call unconditionally on every startup (a no-op if none was set).

    drop_pending_updates=true is essential here, not optional: if this
    bot token was EVER polled via plain Bot-API getUpdates (by this bot,
    an earlier version of it, or any other project sharing the token),
    Telegram queues updates server-side until they're acknowledged with
    an offset. That queue blocks MTProto (Pyrogram) from receiving NEW
    updates too -- the bot looks fully connected in the logs, but every
    message just piles up server-side instead of arriving. Dropping the
    backlog on every boot keeps that queue from ever getting stuck again.
    """
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/deleteWebhook"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, params={"drop_pending_updates": "true"}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
        if data.get("ok"):
            logger.info("deleteWebhook (dropped pending updates): ok (result=%s)", data.get("result"))
        else:
            logger.warning("deleteWebhook returned an error: %s", data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not call deleteWebhook (continuing anyway): %s", exc)


async def _confirm_restart(app: Client) -> None:
    """If /restart left a note behind (see handlers.restart_cmd), this is
    the first thing the new process does once it's back online: edit that
    "🔄 Restarting..." message into a confirmation, then clean up the note
    so a normal crash-restart doesn't also try to edit a stale message."""
    if not os.path.exists(config.RESTART_STATE_PATH):
        return
    try:
        with open(config.RESTART_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        await app.edit_message_text(state["chat_id"], state["message_id"], "✅ Restarted successfully!")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not confirm restart: %s", exc)
    finally:
        try:
            os.remove(config.RESTART_STATE_PATH)
        except OSError:
            pass


async def _run(app: Client) -> None:
    await _clear_webhook()
    await app.start()
    await _confirm_restart(app)
    try:
        me = await app.get_me()
        logger.info("Bot started as @%s (id=%s)", me.username, me.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bot started, but get_me() failed: %s", exc)
    await idle()
    await app.stop()


def main() -> None:
    try:
        config.validate_required_for_runtime()
    except RuntimeError as exc:
        logger.error(str(exc))
        sys.exit(1)

    app = build_client()
    register_handlers(app)
    register_settings_handlers(app)

    # Local, localhost-only daemon that powers torrent/magnet downloads.
    # No-ops entirely if config.TORRENT_ENABLED is False (see config.py) --
    # useful on hosts that disallow torrenting. If aria2c isn't installed
    # this just logs a warning and torrent commands report a clean error
    # -- direct-link leeching still works either way.
    start_aria2_daemon()

    logger.info("Starting leech bot")
    try:
        asyncio.run(_run(app))
    finally:
        stop_aria2_daemon()


if __name__ == "__main__":
    main()
