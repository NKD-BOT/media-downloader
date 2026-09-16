"""
ytdlp.py
Downloads from YouTube and the hundreds of other sites yt-dlp supports.
Deliberately does nothing beyond producing a local file -- naming,
thumbnail, metadata, caption, splitting, upload, and MediaInfo are all
handled by the exact same /usetting pipeline a direct link goes through
(see handlers._process_downloaded_file), so every feature already built
for direct-link leeching works identically here for free.
"""

import asyncio
import logging
import os
from typing import Callable, Tuple

logger = logging.getLogger("leech.ytdlp")

try:
    import yt_dlp
    _YTDLP_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover
    yt_dlp = None  # type: ignore[assignment]
    _YTDLP_IMPORT_ERROR = str(exc)

# Known yt-dlp-supported sites worth routing here instead of treating the
# link as a plain direct-file download. Extend as needed -- yt-dlp itself
# supports 1000+ sites, but most /leech links are either direct file URLs
# or magnets, so an allowlist avoids accidentally trying (and failing)
# yt-dlp extraction on an ordinary file-hosting link.
_KNOWN_DOMAINS = (
    "youtube.com", "youtu.be", "music.youtube.com",
    "twitter.com", "x.com", "instagram.com", "facebook.com", "fb.watch",
    "tiktok.com", "reddit.com", "vimeo.com", "dailymotion.com",
    "twitch.tv", "soundcloud.com", "streamable.com",
)


class YtdlpError(Exception):
    pass


class Cancelled(Exception):
    pass


def ytdlp_available() -> bool:
    return yt_dlp is not None


def is_ytdlp_source(url: str) -> bool:
    """True for links that should go through yt-dlp instead of a plain
    direct-file download. Magnet links and .torrent URLs never reach
    here -- handlers.py checks those first. Always False if the yt-dlp
    package itself isn't installed, so such links just fall through to
    the plain direct-link path (which will fail with a clear error)
    instead of the bot crashing."""
    if yt_dlp is None:
        return False
    url = (url or "").strip().lower()
    if not url.startswith(("http://", "https://")):
        return False
    return any(domain in url for domain in _KNOWN_DOMAINS)


async def download(job, url: str, dest_dir: str, on_progress: Callable[[int, int], None]) -> Tuple[str, str]:
    """Downloads `url` via yt-dlp into dest_dir, picking the best
    available video+audio (merged to .mkv if they come as separate
    streams -- requires ffmpeg, already a dependency here).

    Returns (file_path, title). Raises YtdlpError on failure, or
    Cancelled if job.cancelled becomes True mid-download (checked from
    yt-dlp's own progress hook, so it can stop promptly rather than
    running to completion first)."""
    if yt_dlp is None:
        raise YtdlpError(f"yt-dlp package isn't installed ({_YTDLP_IMPORT_ERROR}).")

    os.makedirs(dest_dir, exist_ok=True)
    result: dict = {}

    def hook(d: dict) -> None:
        if job.cancelled:
            raise Cancelled()
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            job.downloaded = downloaded
            job.total = total
            on_progress(downloaded, total)
        elif d.get("status") == "finished":
            result["path"] = d.get("filename")

    ydl_opts = {
        "outtmpl": os.path.join(dest_dir, "%(title).150B [%(id)s].%(ext)s"),
        "format": "bv*+ba/b",  # best video+audio if separate, else best combined
        "merge_output_format": "mkv",
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "restrictfilenames": False,
    }

    def _run() -> Tuple[str, str]:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = result.get("path") or ydl.prepare_filename(info)

            # merge_output_format can change the final extension from
            # what prepare_filename() predicted before merging happened.
            if not os.path.exists(path):
                base, _ = os.path.splitext(path)
                for ext in (".mkv", ".mp4", ".webm", ".m4a", ".mp3"):
                    if os.path.exists(base + ext):
                        path = base + ext
                        break

            return path, (info.get("title") or os.path.basename(path))

    try:
        path, title = await asyncio.to_thread(_run)
    except Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        raise YtdlpError(str(exc)) from exc

    if not path or not os.path.exists(path):
        raise YtdlpError("yt-dlp reported success but no output file was found.")

    return path, title
