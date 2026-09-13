"""
metadata.py
Optional metadata-title embedding for leeched audio/video files, done via
a local `ffmpeg` binary using a stream-copy remux ("-c copy" -- no
re-encoding, so it's fast and lossless). If ffmpeg isn't installed, the
file isn't a recognized audio/video type, or the remux fails for any
reason, the original file is returned unchanged -- this feature never
blocks or breaks a leech job.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger("leech.metadata")

_MEDIA_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v",
    ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus",
}

# ffprobe's format_name -> the extension we should correct a file to when
# its container is really media but its given filename says otherwise
# (some release sites disguise videos with e.g. a ".zip" name to dodge
# naive filename-based filters).
_FORMAT_NAME_TO_EXT = {
    "matroska,webm": ".mkv",
    "mov,mp4,m4a,3gp,3g2,mj2": ".mp4",
    "avi": ".avi",
    "asf": ".wmv",
    "flv": ".flv",
    "mp3": ".mp3",
    "wav": ".wav",
    "ogg": ".ogg",
    "flac": ".flac",
}


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def is_media_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _MEDIA_EXTS


async def embed_title(path: str, title: str) -> str:
    """Remuxes `path` with an embedded metadata title tag. Returns the
    path to actually upload: a new `<name>.meta<ext>` file on success, or
    the original `path` unchanged if embedding wasn't possible/failed.
    Callers are expected to have already checked the file's real/intended
    name looks like media (is_media_file on the *display* name) -- this
    function doesn't re-check by `path`'s own extension, since `path` is
    often a generic temp filename (e.g. ending in .tmp) that would never
    match regardless of what the file actually contains."""
    if not title:
        return path
    if not ffmpeg_available():
        logger.info("ffmpeg not found on PATH -- skipping metadata embed for %s", path)
        return path

    out_path = path + ".meta" + os.path.splitext(path)[1]
    cmd = [
        "ffmpeg", "-y", "-i", path,
        "-map", "0", "-c", "copy",
        "-metadata", f"title={title}",
        out_path,
    ]

    def _run() -> None:
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900
        )
        if result.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError(result.stderr.decode(errors="ignore")[-500:])

    try:
        await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Metadata embed failed for %s: %s", path, exc)
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass
        return path

    try:
        os.remove(path)
    except OSError:
        pass
    return out_path


# Common ISO 639-1/639-2 language codes -> readable names, since ffprobe
# reports raw tags like "hin"/"eng" but captions look much nicer showing
# "Hindi"/"English" (matching what most reference leech bots display).
_LANGUAGE_NAMES = {
    "eng": "English", "hin": "Hindi", "tam": "Tamil", "tel": "Telugu",
    "kan": "Kannada", "mal": "Malayalam", "ben": "Bengali", "guj": "Gujarati",
    "mar": "Marathi", "pan": "Punjabi", "urd": "Urdu", "ori": "Odia",
    "asm": "Assamese", "spa": "Spanish", "fre": "French", "fra": "French",
    "ger": "German", "deu": "German", "ita": "Italian", "por": "Portuguese",
    "rus": "Russian", "chi": "Chinese", "zho": "Chinese", "jpn": "Japanese",
    "kor": "Korean", "ara": "Arabic", "tur": "Turkish", "vie": "Vietnamese",
    "tha": "Thai", "ind": "Indonesian", "nld": "Dutch", "dut": "Dutch",
    "pol": "Polish", "ukr": "Ukrainian", "swe": "Swedish", "nor": "Norwegian",
    "dan": "Danish", "fin": "Finnish", "heb": "Hebrew", "gre": "Greek", "ell": "Greek",
}


def _readable_lang(tag: str) -> str:
    tag = (tag or "und").strip().lower()
    if tag in ("und", "unk", ""):
        return "Unknown"
    return _LANGUAGE_NAMES.get(tag, tag.upper())


async def probe_tracks(path: str) -> Dict[str, List[str]]:
    """Returns {"languages": [...], "subtitles": [...]} -- readable
    language names (e.g. "Hindi", "English") for every embedded audio /
    subtitle track, via `ffprobe`. Powers the {languages}/{subtitles}
    placeholders in a /usetting leech caption. Empty lists if ffprobe is
    missing, the file isn't recognized media, or probing fails -- this
    never blocks a leech job. (Run /sysinfo to check whether ffprobe is
    actually installed on this deployment if these always come back
    empty.)"""
    if not ffprobe_available():
        return {"languages": [], "subtitles": []}

    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=index,codec_type:stream_tags=language",
        "-of", "json", path,
    ]

    def _run() -> bytes:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
        return result.stdout

    try:
        raw = await asyncio.to_thread(_run)
        data = json.loads(raw or b"{}")
    except Exception as exc:  # noqa: BLE001
        logger.info("ffprobe failed for %s: %s", path, exc)
        return {"languages": [], "subtitles": []}

    languages: List[str] = []
    subtitles: List[str] = []
    for stream in data.get("streams", []):
        lang = _readable_lang((stream.get("tags") or {}).get("language", "und"))
        codec_type = stream.get("codec_type")
        if codec_type == "audio":
            languages.append(lang)
        elif codec_type == "subtitle":
            subtitles.append(lang)
    return {"languages": languages, "subtitles": subtitles}


async def detect_real_container(path: str) -> Optional[str]:
    """Probes a file's actual bytes -- regardless of its given extension
    -- to see if it's really a playable video/audio container. Some
    release sites disguise videos with a misleading extension (commonly
    `.zip`) to dodge naive filename-based filters; this lets a leech
    still upload it as native video/audio and read its embedded
    audio/subtitle languages, instead of it silently being treated as an
    opaque document forever.

    Returns a corrected extension (e.g. '.mkv') if the real content is
    recognized media, else None -- including when it's genuinely a
    zip/rar archive, ffprobe is unavailable, or the format is unknown.
    Never raises."""
    if not ffprobe_available():
        return None

    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=format_name",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]

    def _run() -> str:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        return result.stdout.decode(errors="ignore").strip()

    try:
        format_name = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        logger.info("Container detection failed for %s: %s", path, exc)
        return None

    return _FORMAT_NAME_TO_EXT.get(format_name)


async def probe_video_info(path: str) -> Optional[Dict[str, int]]:
    """Returns {'duration': seconds, 'width': px, 'height': px} for a
    video file via ffprobe, or None if ffprobe is unavailable, the file
    has no video stream, or probing fails.

    Without these, Telegram clients often show a video with a blank
    preview and a "0:00" duration even though the file itself plays
    perfectly fine once opened -- passing them on upload fixes that."""
    if not ffprobe_available():
        return None

    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", path,
    ]

    def _run() -> bytes:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        return result.stdout

    try:
        raw = await asyncio.to_thread(_run)
        data = json.loads(raw or b"{}")
    except Exception as exc:  # noqa: BLE001
        logger.info("Video info probe failed for %s: %s", path, exc)
        return None

    streams = data.get("streams") or [{}]
    fmt = data.get("format") or {}
    width = streams[0].get("width")
    height = streams[0].get("height")
    if not width or not height:
        return None
    try:
        duration = int(float(fmt.get("duration", 0)))
    except (TypeError, ValueError):
        duration = 0

    return {"duration": duration, "width": int(width), "height": int(height)}


async def generate_thumbnail(path: str) -> Optional[str]:
    """Extracts a single frame from a video as a JPEG thumbnail, for
    uploads where the user hasn't set a custom /usetting thumbnail --
    without any thumbnail at all, Telegram's own auto-generation is
    unreliable and often leaves the preview blank. Returns the new
    thumbnail's path, or None if ffmpeg is unavailable or extraction
    fails (never blocks the leech itself). The caller is responsible for
    deleting the returned path after the upload -- unlike a user's
    persistent /usetting thumbnail, this one is single-use."""
    if not ffmpeg_available():
        return None

    out_path = path + "_thumb.jpg"
    cmd = [
        "ffmpeg", "-y", "-ss", "00:00:01", "-i", path,
        "-frames:v", "1", "-vf", "scale=320:-1",
        out_path,
    ]

    def _run() -> None:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        if result.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError(result.stderr.decode(errors="ignore")[-300:])

    try:
        await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        logger.info("Thumbnail generation failed for %s: %s", path, exc)
        return None

    return out_path
