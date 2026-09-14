"""
downloader.py
Streaming HTTP(S) download with progress callbacks, a basic SSRF guard
(refuses to fetch private/loopback/link-local addresses -- relevant
because this bot fetches arbitrary user-supplied URLs from a cloud host),
and a simple binary file splitter for files over Telegram's per-file
upload limit.
"""

import asyncio
import os
import time
import socket
import ipaddress
import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from urllib.parse import urlparse

import aiohttp
import aiofiles

import config

logger = logging.getLogger("leech.downloader")

ProgressCallback = Callable[[int, int], None]  # (downloaded_bytes, total_bytes) -> None

# Plenty of hosts (CDNs, storage buckets, anti-bot fronts) reject or --
# worse -- silently hang requests that carry aiohttp's default
# "Python/3.x aiohttp/x.y" User-Agent. Sending an ordinary browser UA
# avoids the vast majority of those false-positive blocks.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


class DownloadError(Exception):
    pass


class Cancelled(Exception):
    pass


def is_safe_url(url: str) -> Optional[str]:
    """Returns None if the URL is safe to fetch, otherwise a human-readable
    reason it was refused. Blocks non-http(s) schemes and any hostname that
    resolves to a private/loopback/link-local/multicast address, so this
    bot can't be used to reach internal services (e.g. cloud metadata
    endpoints) from wherever it's hosted."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "That doesn't look like a valid URL."

    if parsed.scheme not in ("http", "https"):
        return "Only http:// and https:// links are supported."
    if not parsed.hostname:
        return "That doesn't look like a valid URL."

    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return f"Could not resolve host: {parsed.hostname}"

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return "This URL resolves to a private/internal address, which isn't allowed."

    return None


@dataclass
class Job:
    user_id: int
    url: str
    dest_path: str = ""
    downloaded: int = 0
    total: int = 0
    phase: str = "starting"  # starting -> downloading -> splitting -> uploading -> done
    cancelled: bool = False
    part_paths: List[str] = field(default_factory=list)
    upload_index: int = 0
    upload_count: int = 1
    start_time: float = field(default_factory=time.time)
    # Unique per-job token so an old Stop button left over from a previous,
    # already-finished job can't accidentally cancel a brand new one --
    # Telegram buttons stay tappable forever, but they only carry the
    # user_id unless we also check this.
    token: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


async def download_file(job: Job, url: str, dest_path: str, on_progress: ProgressCallback) -> None:
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    timeout = aiohttp.ClientTimeout(
        total=config.REQUEST_TIMEOUT_SECONDS,
        connect=config.CONNECT_TIMEOUT_SECONDS,
        sock_connect=config.CONNECT_TIMEOUT_SECONDS,
        sock_read=config.SOCK_READ_TIMEOUT_SECONDS,
    )

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise DownloadError(f"Server returned HTTP {resp.status}")

                total = int(resp.headers.get("Content-Length", 0))
                job.total = total
                content_disposition = resp.headers.get("Content-Disposition")

                downloaded = 0
                async with aiofiles.open(dest_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(config.DOWNLOAD_CHUNK_SIZE):
                        if job.cancelled:
                            raise Cancelled()
                        await f.write(chunk)
                        downloaded += len(chunk)
                        job.downloaded = downloaded
                        on_progress(downloaded, total)

                # Some CDNs / anti-bot fronts silently cut the connection
                # after serving only the first few MB, without aiohttp ever
                # raising an error -- the stream just ends early. Without
                # this check that produces a truncated file that still gets
                # uploaded as if it succeeded (broken video, wrong
                # duration/size). Treat a short read as a hard failure.
                if total > 0 and downloaded < total:
                    raise DownloadError(
                        f"Incomplete download: got {downloaded} of {total} bytes -- "
                        "the server cut the connection short (common with anti-bot/"
                        "rate-limiting CDNs). Try again in a bit, or use a different link."
                    )

                job.content_disposition = content_disposition  # type: ignore[attr-defined]

    except asyncio.TimeoutError as exc:
        raise DownloadError(
            f"Connection timed out after {config.CONNECT_TIMEOUT_SECONDS}s "
            "-- the server took too long to respond (it may be blocking "
            "automated requests, or the link may be dead)."
        ) from exc
    except aiohttp.ClientConnectorError as exc:
        raise DownloadError(f"Could not connect to the server: {exc}") from exc
    except aiohttp.ClientError as exc:
        raise DownloadError(f"Network error while downloading: {exc}") from exc


def split_file(src_path: str, part_size_bytes: int) -> List[str]:
    """Splits src_path into equal-size chunks (last one may be smaller),
    named `<original>.part001`, `<original>.part002`, ... Returns the list
    of part paths in order. The original file is left untouched --
    callers should delete it themselves once parts are confirmed uploaded."""
    parts: List[str] = []
    with open(src_path, "rb") as src:
        index = 1
        while True:
            chunk = src.read(part_size_bytes)
            if not chunk:
                break
            part_path = f"{src_path}.part{index:03d}"
            with open(part_path, "wb") as out:
                out.write(chunk)
            parts.append(part_path)
            index += 1
    return parts


def cleanup_paths(paths: List[str]) -> None:
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", p, exc)
