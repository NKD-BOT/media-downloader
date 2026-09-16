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
        # Pyrofork sends/receives a file's parts sequentially over ONE
        # connection by default (max 1). Raising this lets it use several
        # connections at once for both uploads and downloads-to-the-bot
        # (e.g. reading an uploaded .torrent/thumbnail), often multiplying
        # real-world Telegram transfer speed.
        max_concurrent_transmissions=config.MAX_CONCURRENT_TRANSMISSIONS,
    )


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
    await app.start()
    await _confirm_restart(app)
    logger.info("Bot started")
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
