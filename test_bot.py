"""
test_bot.py — MINIMAL isolation test.
Only purpose: prove whether THIS Railway project + THIS bot token can
receive a message and send a reply at all, with zero other code in the
way (no aria2, no mongo, no settings, no other handlers).

Run this INSTEAD of main.py temporarily to bisect the problem:
  - If /start replies here -> the bug is somewhere in the real bot's
    handlers.py / other modules (something swallows or blocks it).
  - If /start does NOT reply here either -> the problem is environmental
    (Railway networking, account, or token) and NOT in the bot's code.
"""

import logging
from pyrogram import Client, filters, idle
import config  # reuses the same API_ID / API_HASH / BOT_TOKEN

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("test_bot")

app = Client(
    name="test_bot_session",   # different session name on purpose -- avoids
                                 # reusing/clashing with the real bot's session file
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
)


@app.on_message(filters.all)
async def catch_all(client, message):
    logger.info("TESTBOT GOT MESSAGE: chat=%s text=%r", message.chat.id, message.text)
    try:
        await message.reply_text("✅ test_bot alive and replying!")
        logger.info("TESTBOT: reply sent OK")
    except Exception:
        logger.exception("TESTBOT: reply FAILED")


async def main():
    await app.start()
    me = await app.get_me()
    logger.info("TESTBOT started as @%s (id=%s)", me.username, me.id)
    await idle()
    await app.stop()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
