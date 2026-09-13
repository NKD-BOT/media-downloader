"""
config.py
Centralized configuration for the direct-link leech bot. Everything is
read from environment variables (via python-dotenv for local dev). No
secret is ever hardcoded here.
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("leech.config")


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Env var %s is not a valid int (%r); using default %s", name, raw, default)
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---- Telegram credentials (Pyrogram/Pyrofork) ----
API_ID: int = _get_int("API_ID", 20599570)
API_HASH: str = os.getenv("API_HASH", "0d5a7f73ea37f1d11dd470cda9a1a75f")
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "7357607802:AAHdbb0ygtAJ6jMwbCjGGrqpgGmmcLjVOOU")

# ---- Owner / access control ----
OWNER_ID: int = _get_int("OWNER_ID", 6858251193)
# Comma-separated list of Telegram user IDs allowed to use the bot.
# Leave empty to allow anyone who can message the bot.
_raw_allowed = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = {int(x.strip()) for x in _raw_allowed.split(",") if x.strip().isdigit()}

# ---- Storage / limits ----
DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "downloads")
# Pyrofork (MTProto) can upload much larger files than the plain Bot API,
# but a non-Premium account/bot is still capped at 2000 MiB per file.
MAX_FILE_SIZE_MB: int = _get_int("MAX_FILE_SIZE_MB", 1990)
# If a downloaded file is bigger than this, split it into equal chunks of
# this size (in MiB) instead of refusing it outright. Set equal to
# MAX_FILE_SIZE_MB (default) to always split oversized files; set to 0 to
# disable splitting and just refuse oversized files.
SPLIT_SIZE_MB: int = _get_int("SPLIT_SIZE_MB", 1990)
DOWNLOAD_CHUNK_SIZE: int = 1024 * 1024  # 1 MiB streaming chunks
REQUEST_TIMEOUT_SECONDS: int = _get_int("REQUEST_TIMEOUT_SECONDS", 3600)
# Separate, much shorter timeouts for establishing the connection and for
# any single stalled read -- without these, a server that silently hangs
# (rather than cleanly refusing) would make a /leech look "stuck" for up
# to the full REQUEST_TIMEOUT_SECONDS before failing.
CONNECT_TIMEOUT_SECONDS: int = _get_int("CONNECT_TIMEOUT_SECONDS", 30)
SOCK_READ_TIMEOUT_SECONDS: int = _get_int("SOCK_READ_TIMEOUT_SECONDS", 120)
PROGRESS_EDIT_INTERVAL_SECONDS: float = 3.0

# One leech job at a time per user; additional /leech requests while busy
# are rejected with a clear message (no hidden queueing).
MAX_CONCURRENT_JOBS_PER_USER: int = 1

# ---- Torrent / magnet support (via a local aria2c daemon) ----
# Master on/off switch. Set TORRENT_ENABLED=false to fully disable
# magnet/torrent leeching -- the aria2c daemon is never even started, so
# no P2P/DHT traffic goes out at all. Handy for hosts (e.g. Railway) whose
# acceptable-use policy disallows torrenting; direct-link /leech and
# everything else keeps working normally. The torrent code itself is
# untouched -- just flip this back to true to turn it on again.
TORRENT_ENABLED: bool = _get_bool("TORRENT_ENABLED", false)
# Port the bot's own private aria2c RPC daemon listens on. Only needs to
# be reachable from this process, not exposed publicly.
ARIA2_RPC_PORT: int = _get_int("ARIA2_RPC_PORT", 6800)
# Optional shared secret for the local RPC connection. Not required since
# the daemon only listens on localhost, but supported if you want it.
ARIA2_RPC_SECRET: str = os.getenv("ARIA2_RPC_SECRET", "")
# How long (seconds) to wait for a magnet link to resolve its metadata
# (file list) before giving up and proceeding anyway.
TORRENT_METADATA_TIMEOUT_SECONDS: int = _get_int("TORRENT_METADATA_TIMEOUT_SECONDS", 60)


def validate_required_for_runtime() -> None:
    missing = []
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing) +
            ". Copy .env.example to .env and fill in the values."
        )
