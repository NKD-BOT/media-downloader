"""
torrent.py
Torrent / magnet-link support via a local aria2c daemon. aria2 speaks the
actual BitTorrent protocol; this module just starts/manages the daemon,
hands it magnet URIs / .torrent files / .torrent URLs, and polls progress
in a shape that mirrors downloader.Job so handlers.py can treat both
kinds of jobs the same way for /status and /cancel.

The daemon is started once at bot startup (see main.py) and only listens
on localhost -- it is never exposed to the network.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import aria2p

import config

logger = logging.getLogger("leech.torrent")

_aria2_process: Optional[subprocess.Popen] = None
_api: Optional[aria2p.API] = None


class TorrentError(Exception):
    pass


class TorrentCancelled(Exception):
    pass


def aria2_available() -> bool:
    return shutil.which("aria2c") is not None


def torrent_support_enabled() -> bool:
    return config.TORRENT_ENABLED and _api is not None


def torrent_disabled_reason() -> str:
    """Human-readable reason torrent/magnet leech isn't available right
    now, for handlers.py to show the user -- distinguishes "turned off on
    purpose" from "aria2c isn't installed"."""
    if not config.TORRENT_ENABLED:
        return "Torrent/magnet leeching is turned off on this bot (TORRENT_ENABLED=false)."
    return "Torrent support isn't available on this bot (aria2c isn't installed)."


def is_torrent_source(text: str) -> bool:
    """True for magnet links and URLs/paths that look like a .torrent
    file. Used by handlers.py to decide which pipeline to use."""
    text = (text or "").strip().lower()
    return text.startswith("magnet:?") or text.endswith(".torrent")


def start_aria2_daemon() -> None:
    """Starts a local, localhost-only aria2c RPC daemon. Safe to call
    even if aria2c isn't installed -- torrent commands will then report a
    clear error instead of the bot crashing on startup.

    Does nothing at all if config.TORRENT_ENABLED is False -- aria2c is
    never even launched, so the bot generates zero P2P/DHT/BitTorrent
    traffic. That matters on hosts (e.g. Railway) whose acceptable-use
    policy disallows torrenting; flip TORRENT_ENABLED back to true
    whenever you move somewhere that does allow it."""
    global _aria2_process, _api

    if not config.TORRENT_ENABLED:
        logger.info("TORRENT_ENABLED=false -- skipping aria2c startup; torrent/magnet leech is disabled.")
        return

    if not aria2_available():
        logger.warning("aria2c not found on PATH -- torrent/magnet support disabled.")
        return

    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    cmd = [
        "aria2c",
        "--enable-rpc",
        "--rpc-listen-all=false",  # localhost only -- never exposed externally
        f"--rpc-listen-port={config.ARIA2_RPC_PORT}",
        f"--dir={config.DOWNLOAD_DIR}",
        "--bt-stop-timeout=0",
        "--seed-time=0",              # leech only, never seed after completion
        "--max-overall-upload-limit=1K",
        "--follow-torrent=mem",
        "--summary-interval=0",
        "--allow-overwrite=true",
        "--file-allocation=none",
        "--check-certificate=true",
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "--connect-timeout=30",
        "--timeout=60",
    ]
    if config.ARIA2_RPC_SECRET:
        cmd.append(f"--rpc-secret={config.ARIA2_RPC_SECRET}")

    try:
        _aria2_process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.error("Could not start aria2c: %s", exc)
        return

    _api = aria2p.API(
        aria2p.Client(
            host="http://localhost",
            port=config.ARIA2_RPC_PORT,
            secret=config.ARIA2_RPC_SECRET,
        )
    )

    for _ in range(50):  # wait up to ~5s for the daemon to come up
        try:
            _api.get_stats()
            logger.info("aria2c daemon up on localhost:%s -- torrent/magnet support enabled.", config.ARIA2_RPC_PORT)
            return
        except Exception:
            time.sleep(0.1)

    logger.error("aria2c did not respond in time -- torrent support may not work.")
    _api = None


def stop_aria2_daemon() -> None:
    global _aria2_process
    if _aria2_process and _aria2_process.poll() is None:
        _aria2_process.terminate()
        try:
            _aria2_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _aria2_process.kill()
    _aria2_process = None


@dataclass
class TorrentJob:
    user_id: int
    source: str
    gid: str = ""
    downloaded: int = 0
    total: int = 0
    phase: str = "starting"  # starting -> metadata -> downloading -> uploading -> done
    cancelled: bool = False
    start_time: float = field(default_factory=time.time)
    file_paths: List[str] = field(default_factory=list)
    upload_index: int = 0
    upload_count: int = 1
    seeders: int = 0
    connections: int = 0
    # Unique per-job token so an old Stop button left over from a previous,
    # already-finished job can't accidentally cancel a brand new one.
    token: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


async def add_torrent(job: TorrentJob, source: str, torrent_file_path: Optional[str] = None) -> None:
    """Adds a magnet URI, a .torrent URL, or a local .torrent file to
    aria2. Raises TorrentError if aria2 isn't available or the add fails."""
    if _api is None:
        raise TorrentError("Torrent support isn't available on this bot (aria2c isn't installed).")

    def _add():
        options = {"dir": config.DOWNLOAD_DIR}
        if torrent_file_path:
            return _api.add_torrent(torrent_file_path, options=options)
        if source.strip().lower().startswith("magnet:?"):
            return _api.add_magnet(source, options=options)
        return _api.add_uris([source], options=options)  # direct .torrent URL

    try:
        download = await asyncio.to_thread(_add)
    except Exception as exc:  # noqa: BLE001
        raise TorrentError(f"could not add torrent: {exc}") from exc

    job.gid = download.gid


async def wait_for_metadata(job: TorrentJob) -> None:
    """Magnet links start as a metadata-only fetch; once aria2 resolves the
    real file list it hands off to a new gid. Waits for that handoff (or
    times out and proceeds with whatever gid is active)."""
    if _api is None:
        return
    deadline = time.time() + config.TORRENT_METADATA_TIMEOUT_SECONDS
    while time.time() < deadline:
        if job.cancelled:
            raise TorrentCancelled()

        def _get():
            return _api.get_download(job.gid)

        try:
            d = await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            raise TorrentError(str(exc)) from exc

        if d.followed_by_ids:
            job.gid = d.followed_by_ids[0]
            break
        if not d.is_metadata:
            break
        await asyncio.sleep(1)

    job.phase = "downloading"


async def poll_progress(job: TorrentJob, on_progress) -> None:
    """Polls aria2 until the download completes, errors, or is cancelled."""
    if _api is None:
        raise TorrentError("Torrent support isn't available on this bot.")

    while True:
        if job.cancelled:
            def _remove():
                try:
                    _api.remove([_api.get_download(job.gid)], files=True)
                except Exception:
                    pass
            await asyncio.to_thread(_remove)
            raise TorrentCancelled()

        def _get():
            return _api.get_download(job.gid)

        try:
            d = await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001
            raise TorrentError(str(exc)) from exc

        if d.status == "error":
            raise TorrentError(d.error_message or "unknown aria2 error")

        job.downloaded = d.completed_length
        job.total = d.total_length
        try:
            job.seeders = d.num_seeders or 0
        except Exception:  # noqa: BLE001
            job.seeders = 0
        try:
            job.connections = d.connections or 0
        except Exception:  # noqa: BLE001
            job.connections = 0
        on_progress(d.completed_length, d.total_length)

        if d.is_complete:
            job.file_paths = [f.path for f in d.files if f.selected and f.length > 0]
            return

        await asyncio.sleep(2)
