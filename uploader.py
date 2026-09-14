"""
uploader.py
Uploads a finished file to Telegram, choosing a native media type
(video/audio/photo) unless the user opted for plain documents via
/usetting, and attaching a custom thumbnail when one is configured.
Targets an explicit chat_id (rather than always replying in whatever
chat the /leech command came from) so a leech started in a group can be
delivered to the user's PM instead.
"""

import logging
import os
from typing import Callable, Optional

from pyrogram import Client
from pyrogram.types import Message

logger = logging.getLogger("leech.uploader")

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v"}
_AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus"}
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


async def upload_file(
    client: Client,
    chat_id: int,
    path: str,
    file_name: str,
    caption: str,
    thumb: Optional[str],
    send_as_document: bool,
    progress: Callable[[int, int], None],
    duration: int = 0,
    width: int = 0,
    height: int = 0,
) -> Message:
    """Sends `path` to `chat_id`. Falls back to a plain document if a
    native-media send is attempted but rejected by Telegram (e.g. a
    mislabeled/corrupt video), so a leech never fails outright just
    because of the media-type guess.

    duration/width/height (when known, e.g. via metadata.probe_video_info)
    are passed straight to Telegram -- without them, clients often show a
    blank preview and a "0:00" duration for an otherwise perfectly playable
    video."""
    ext = os.path.splitext(file_name)[1].lower()
    thumb_arg = thumb if (thumb and os.path.exists(thumb)) else None

    async def _send_native() -> Optional[Message]:
        if send_as_document:
            return None
        try:
            if ext in _VIDEO_EXTS:
                return await client.send_video(
                    chat_id, path, file_name=file_name, thumb=thumb_arg,
                    duration=duration, width=width, height=height,
                    caption=caption, progress=progress,
                )
            if ext in _AUDIO_EXTS:
                return await client.send_audio(
                    chat_id, path, file_name=file_name, thumb=thumb_arg,
                    duration=duration,
                    caption=caption, progress=progress,
                )
            if ext in _PHOTO_EXTS:
                return await client.send_photo(chat_id, path, caption=caption, progress=progress)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Native upload failed for %s (%s); falling back to document.", file_name, exc)
        return None

    sent = await _send_native()
    if sent is not None:
        return sent

    return await client.send_document(
        chat_id, path, file_name=file_name, thumb=thumb_arg,
        caption=caption, progress=progress,
    )
