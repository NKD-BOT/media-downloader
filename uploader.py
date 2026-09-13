"""
uploader.py
Uploads a finished file to Telegram, choosing a native media type
(video/audio/photo) unless the user opted for plain documents via
/usetting, and attaching a custom thumbnail when one is configured.
"""

import logging
import os
from typing import Callable, Optional

from pyrogram.types import Message

logger = logging.getLogger("leech.uploader")

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v"}
_AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus"}
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


async def upload_file(
    message: Message,
    path: str,
    file_name: str,
    caption: str,
    thumb: Optional[str],
    send_as_document: bool,
    progress: Callable[[int, int], None],
) -> Message:
    """Sends `path` as a reply in `message`'s chat. Falls back to a plain
    document if a native-media send is attempted but rejected by Telegram
    (e.g. a mislabeled/corrupt video), so a leech never fails outright
    just because of the media-type guess."""
    ext = os.path.splitext(file_name)[1].lower()
    thumb_arg = thumb if (thumb and os.path.exists(thumb)) else None

    async def _send_native() -> Optional[Message]:
        if send_as_document:
            return None
        try:
            if ext in _VIDEO_EXTS:
                return await message.reply_video(
                    path, file_name=file_name, thumb=thumb_arg,
                    caption=caption, progress=progress,
                )
            if ext in _AUDIO_EXTS:
                return await message.reply_audio(
                    path, file_name=file_name, thumb=thumb_arg,
                    caption=caption, progress=progress,
                )
            if ext in _PHOTO_EXTS:
                return await message.reply_photo(path, caption=caption, progress=progress)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Native upload failed for %s (%s); falling back to document.", file_name, exc)
        return None

    sent = await _send_native()
    if sent is not None:
        return sent

    return await message.reply_document(
        path, file_name=file_name, thumb=thumb_arg,
        caption=caption, progress=progress,
    )
