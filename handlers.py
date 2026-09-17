"""
handlers.py
Commands:

/leech <url> [filename] — downloads a direct HTTP(S) link and uploads it
    to this chat, splitting into parts automatically if it's bigger than
    Telegram's per-file limit. Shows live progress for both download and
    upload, edited periodically (not on every chunk).
/leech <magnet_link> — downloads a torrent via magnet link and uploads
    every file in it (each split automatically if oversized).
/leech <direct .torrent URL> — same, but fetches the .torrent file first.
/leech as a reply to an uploaded .torrent file — same, using that file.
/cancel — cancels the caller's in-progress job.
/status — shows the caller's current job progress.

One job per user at a time by design -- a second /leech while one is
running is rejected with a clear message rather than silently queued.
"""

import asyncio
import json
import os
import re
import shutil
import time
import logging
import zipfile
from typing import Dict, List, Optional, Tuple, Union

from pyrogram import Client, ContinuePropagation, filters
from pyrogram.enums import ChatType
from pyrogram.types import (
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pyrogram.errors import RPCError, FloodWait

import config
import settings_db
import mongo_db
import auth_db
import metadata as metadata_mod
import telegraph
import ytdlp
from uploader import upload_file
from leech_settings import pop_pending
from downloader import Job, download_file, split_file, cleanup_paths, is_safe_url, DownloadError, Cancelled
from torrent import (
    TorrentJob, TorrentError, TorrentCancelled, is_torrent_source,
    torrent_support_enabled, torrent_disabled_reason, aria2_available, add_torrent, wait_for_metadata, poll_progress,
)
from utils import (
    format_status_block, human_size, filename_from_response, split_path_parts,
    apply_naming, build_caption,
)

logger = logging.getLogger("leech.handlers")

AnyJob = Union[Job, TorrentJob]
_active_jobs: Dict[int, AnyJob] = {}


def is_allowed(user_id: int) -> bool:
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    if not config.ALLOWED_USER_IDS:
        return True  # no allowlist configured -> open to anyone who can message the bot
    return user_id in config.ALLOWED_USER_IDS


def _is_owner(user_id: int) -> bool:
    return bool(config.OWNER_ID) and user_id == config.OWNER_ID


async def _check_leech_access(message: Message) -> Optional[str]:
    """Gatekeeper specifically for /leech (not /help, /status, etc.):
    - In the bot's PM, only the owner can leech.
    - In a group/supergroup, nobody can leech until the owner has
      authorized that specific chat with /a -- once authorized, the
      existing is_allowed() user allowlist (if any) still applies.
    Returns None if allowed, or the message to show the user if not."""
    user_id = message.from_user.id if message.from_user else 0

    if message.chat.type == ChatType.PRIVATE:
        if _is_owner(user_id):
            return None
        return "⛔ This bot only leeches for its owner in PM. Ask the owner to authorize a group with /a instead."

    if _is_owner(user_id):
        return None
    if not await auth_db.is_authorized(message.chat.id):
        return "⛔ This group isn't authorized yet. Ask the bot owner to run /a here."
    return None


async def _safe_edit(message: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await message.edit_text(text, reply_markup=reply_markup)
        except RPCError:
            pass
    except RPCError:
        pass  # message unchanged / deleted / too many edits -- non-fatal


async def _build_media_caption(path: str, name: str, settings: dict) -> str:
    """Builds the upload caption for one file, only running ffprobe (to
    fill {languages}/{subtitles}) when the user's template actually
    references those placeholders -- keeps the common case fast."""
    template = settings.get("leech_caption", "")
    languages = subtitles = None
    if template and ("{languages}" in template or "{subtitles}" in template) and metadata_mod.is_media_file(name):
        probe = await metadata_mod.probe_tracks(path)
        languages, subtitles = probe["languages"], probe["subtitles"]
    return build_caption(name, os.path.getsize(path), template, languages=languages, subtitles=subtitles)


_VIDEO_EXTS_FOR_PROBE = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v"}


async def _prepare_video_extras(path: str, name: str, settings: dict) -> Tuple[dict, Optional[str], Optional[str]]:
    """For a native video upload, probes duration/width/height and (if the
    user hasn't set a custom /usetting thumbnail) auto-extracts a frame to
    use instead -- without these, Telegram often shows a blank preview and
    a "0:00" duration even though the video itself plays perfectly fine.

    Returns (extra_kwargs_for_upload_file, thumb_path_to_use,
    generated_thumb_to_delete_afterward). The third value is None when the
    thumbnail came from the user's persistent /usetting setting instead of
    being freshly generated -- only a freshly generated one should be
    deleted once the upload is done."""
    ext = os.path.splitext(name)[1].lower()
    if settings.get("send_as_document") or ext not in _VIDEO_EXTS_FOR_PROBE:
        return {}, settings.get("thumbnail_path"), None

    extras: dict = await metadata_mod.probe_video_info(path) or {}

    thumb_path = settings.get("thumbnail_path")
    generated_thumb = None
    if not thumb_path:
        generated_thumb = await metadata_mod.generate_thumbnail(path)
        thumb_path = generated_thumb

    return extras, thumb_path, generated_thumb


async def _auto_mediainfo(client: Client, chat_id: int, path: str, name: str) -> None:
    """Runs MediaInfo on a just-uploaded file and posts the Telegraph
    link, matching how most leech bots auto-attach it after a completed
    leech. Only for whole (unsplit) video/audio files -- a raw split part
    isn't valid media and would just produce a nonsense report. Never
    raises; any failure here is silent (the leech itself already
    succeeded, this is just a nice-to-have extra)."""
    if not metadata_mod.is_media_file(name):
        return
    try:
        report = await metadata_mod.run_mediainfo(path)
        if not report:
            return
        page_url = await telegraph.create_page(name, report)
        if page_url:
            await client.send_message(chat_id, f"📄 *MediaInfo*: [{name}]({page_url})")
    except Exception:  # noqa: BLE001
        logger.exception("Auto-MediaInfo failed for %s", name)


def _stop_keyboard(job: "AnyJob") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop", callback_data=f"cancel:{job.user_id}:{job.token}")]])


async def _resolve_file_target(client: Client, message: Message) -> Optional[int]:
    """Returns the chat_id the leeched file(s) should be uploaded to.
    Commands, status cards, and everything else always stay in whatever
    chat the /leech was sent from -- only the final file delivery is
    redirected to the user's own PM when /leech was used in a
    group/supergroup, to keep the group from being flooded with big
    files. Checked upfront (before the download even starts) so a
    failure here surfaces immediately; returns None (after already
    replying with instructions in the group) if the bot can't message
    that user privately yet."""
    if message.chat.type == ChatType.PRIVATE:
        return message.chat.id

    try:
        await client.send_message(message.from_user.id, "📎 The file(s) from this leech will be sent here.")
    except Exception:  # noqa: BLE001
        me = await client.get_me()
        await message.reply_text(
            "⚠️ I can't message you privately yet -- please start a chat with me first, then try again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Start in PM", url=f"https://t.me/{me.username}")]]),
        )
        return None

    return message.from_user.id


def _user_mention(message: Message) -> str:
    return message.from_user.mention if message.from_user else "Unknown"


def _username(message: Message) -> Optional[str]:
    return message.from_user.username if message.from_user else None


def _status_label(job: "AnyJob") -> str:
    if job.phase == "uploading":
        if job.upload_count > 1:
            return f"Uploading part {job.upload_index}/{job.upload_count}"
        return "Uploading"
    if job.phase == "downloading":
        return "Downloading Torrent" if isinstance(job, TorrentJob) else "Downloading"
    if job.phase == "metadata":
        return "Fetching Metadata"
    if job.phase == "splitting":
        return "Splitting"
    return job.phase.capitalize()


async def _run_status_loop(status_msg: Message, job: "AnyJob", phases: set, engine: str, mode: str, user_mention: str) -> None:
    """Background task: edits status_msg with a rich, card-style progress
    block every PROGRESS_EDIT_INTERVAL_SECONDS for as long as job.phase
    stays in `phases` and the job hasn't been cancelled. Used for both the
    download and upload legs of a leech job. Wrapped in a broad
    try/except with logging so a bug here (or a transient Telegram error)
    can never silently kill the whole card without a trace -- it just
    logs and keeps looping instead."""
    torrent = isinstance(job, TorrentJob)
    while job.phase in phases and not job.cancelled:
        try:
            seeders = leechers = None
            if torrent:
                seeders = job.seeders
                leechers = max(job.connections - job.seeders, 0)
            # For a direct-link download, show whether parallel/segmented
            # mode actually kicked in (and why not, if it didn't) -- lets
            # you tell "still slow because the source throttles regardless
            # of connections" apart from "parallel never engaged".
            current_engine = engine
            dl_mode = getattr(job, "download_mode", "")
            if dl_mode and job.phase == "downloading":
                current_engine = f"{engine} [{dl_mode}]"
            text = format_status_block(
                _status_label(job), job.downloaded, job.total, job.start_time,
                engine=current_engine, mode=mode, user_mention=user_mention, user_id=job.user_id,
                seeders=seeders, leechers=leechers,
                download_dir=config.DOWNLOAD_DIR, filename=job.current_name, phase=job.phase,
            )
            await _safe_edit(status_msg, text, reply_markup=_stop_keyboard(job))
        except Exception:  # noqa: BLE001
            logger.exception("Status loop failed for user %s -- will retry next tick.", job.user_id)
        await asyncio.sleep(config.PROGRESS_EDIT_INTERVAL_SECONDS)


async def _copy_to_dump(client: Client, sent_message: Message, dump_chat_id: Optional[int]) -> None:
    """Copies an already-uploaded message to a user's configured /usetting
    dump chat, if any. Never raises -- a missing/invalid dump chat (bot not
    a member, wrong ID, etc.) shouldn't fail the leech itself."""
    if not dump_chat_id:
        return
    try:
        await client.copy_message(dump_chat_id, sent_message.chat.id, sent_message.id)
    except RPCError as exc:
        logger.warning("Could not copy message to dump chat %s: %s", dump_chat_id, exc)


async def _extract_zip_if_any(status_msg: Message, zip_path: str, settings: dict) -> Optional[List[Tuple[str, str]]]:
    """Used by the /leech -e flag. If zip_path is really a zip archive,
    extracts it and returns a list of (extracted_path, display_name)
    pairs -- naming rules (prefix/suffix/name-swap) applied to each entry
    individually. Returns None if it isn't actually a zip (so the caller
    just uploads the original file unchanged, e.g. if -e was given but the
    link wasn't actually an archive)."""
    is_zip = await asyncio.to_thread(zipfile.is_zipfile, zip_path)
    if not is_zip:
        return None

    await _safe_edit(status_msg, "📦 Extracting zip archive...")
    extract_dir = zip_path + "_extracted"

    def _extract() -> List[Tuple[str, str]]:
        os.makedirs(extract_dir, exist_ok=True)
        results: List[Tuple[str, str]] = []
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                extracted_path = zf.extract(info, path=extract_dir)
                display_name = apply_naming(os.path.basename(info.filename) or "file", settings)
                results.append((extracted_path, display_name))
        return results

    return await asyncio.to_thread(_extract)


def register_handlers(app: Client) -> None:

    @app.on_message(filters.all, group=-1)
    async def _debug_log_every_update(client: Client, message: Message):
        # Runs before every other handler (group=-1) and never stops
        # propagation, so it's a pure diagnostic: if this line never shows
        # up in the logs when you message the bot, Telegram updates aren't
        # reaching this process at all (e.g. a duplicate/orphaned instance
        # elsewhere is soaking them up) -- if it DOES show up but nothing
        # else happens, the bug is in a specific handler/filter instead.
        try:
            chat_id = message.chat.id if message.chat else "?"
            user_id = message.from_user.id if message.from_user else "?"
            text_preview = (message.text or message.caption or "<non-text>")[:80]
            logger.info("INCOMING update: chat=%s user=%s text=%r", chat_id, user_id, text_preview)
        except Exception:  # noqa: BLE001
            logger.exception("Debug logger itself failed")
        raise ContinuePropagation

    @app.on_message(filters.command("start"))
    async def start_cmd(client: Client, message: Message):
        await message.reply_text(
            "👋 Hi! Send me a direct download link, a magnet link, or a "
            ".torrent file and I'll fetch it and upload it here.\n\n"
            "*Usage:* `/leech <url or magnet> [filename]`\n"
            "(`/l` also works as a shortcut for `/leech`)\n"
            "Or reply `/leech` to an uploaded `.torrent` file.\n\n"
            "Files bigger than Telegram's per-file limit are automatically "
            "split into parts and uploaded one after another.\n\n"
            "/cancel — stop your current download/upload\n"
            "/status — see progress of your current job\n"
            "/usetting (or /us) — customize prefix/suffix/caption/thumbnail/metadata/dump/name-swap"
        )

    @app.on_message(filters.command("stats"))
    async def stats_cmd(client: Client, message: Message):
        if not mongo_db.mongo_enabled():
            await message.reply_text(
                "📊 Stats require MongoDB (set DB_URI) -- without it, only your "
                "/usetting preferences are saved (to a local file), not usage history."
            )
            return
        stats = await mongo_db.get_stats(message.from_user.id)
        if not stats:
            await message.reply_text("No leech history yet -- try /leech something first!")
            return
        files = stats.get("files_leeched", 0)
        total_bytes = stats.get("bytes_leeched", 0)
        first_seen = stats.get("first_seen")
        first_seen_str = first_seen.strftime("%Y-%m-%d") if first_seen else "—"
        await message.reply_text(
            f"📊 *Your stats*\n\n"
            f"Files leeched: {files}\n"
            f"Total data: {human_size(total_bytes)}\n"
            f"First used: {first_seen_str}"
        )

    @app.on_message(filters.command("mediainfo"))
    async def mediainfo_cmd(client: Client, message: Message):
        target = message.reply_to_message
        if not target or not (target.video or target.audio or target.document):
            await message.reply_text("Reply to a video, audio, or document with /mediainfo to get its technical details.")
            return

        if not metadata_mod.mediainfo_available():
            await message.reply_text("⚠️ The `mediainfo` tool isn't installed on this deployment. Check /sysinfo.")
            return

        status = await message.reply_text("🔍 Downloading file to analyze...")
        os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
        tmp_path = os.path.join(config.DOWNLOAD_DIR, f"mediainfo_{message.from_user.id}_{int(time.time())}")

        def dl_progress(current: int, total: int) -> None:
            pass  # no live card needed for this -- it's a quick, one-shot analysis

        try:
            await client.download_media(target, file_name=tmp_path, progress=dl_progress)
        except Exception as exc:  # noqa: BLE001
            await _safe_edit(status, f"⚠️ Could not download the file: {exc}")
            return

        if not os.path.exists(tmp_path):
            await _safe_edit(status, "⚠️ Download failed -- nothing to analyze.")
            return

        await _safe_edit(status, "🔎 Running MediaInfo...")
        report = await metadata_mod.run_mediainfo(tmp_path)
        cleanup_paths([tmp_path])

        if not report:
            await _safe_edit(status, "⚠️ MediaInfo couldn't analyze this file.")
            return

        display_name = (
            (target.video and target.video.file_name)
            or (target.audio and target.audio.file_name)
            or (target.document and target.document.file_name)
            or "MediaInfo"
        )

        await _safe_edit(status, "📤 Publishing report...")
        page_url = await telegraph.create_page(display_name, report)

        if page_url:
            await _safe_edit(status, f"📄 *MediaInfo*\n\n🔗 [{display_name}]({page_url})")
        elif len(report) <= 3800:
            # Telegraph unreachable -- fall back to a plain message
            await _safe_edit(status, f"```\n{report}\n```")
        else:
            report_path = tmp_path + "_mediainfo.txt"
            try:
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(report)
                await client.send_document(status.chat.id, report_path, caption="📄 MediaInfo report (too long for a message)")
                await status.delete()
            finally:
                cleanup_paths([report_path])

    @app.on_message(filters.command("sysinfo"))
    async def sysinfo_cmd(client: Client, message: Message):
        # Diagnostic command: helps figure out *why* something like the
        # {languages}/{subtitles} caption placeholders or metadata-title
        # embedding isn't working -- e.g. because a host is building this
        # bot via a builder (like Railway's Nixpacks auto-detection) that
        # skips the Dockerfile's `apt-get install ffmpeg`, silently
        # leaving ffmpeg/ffprobe missing even though the code is fine.
        def check(ok: bool) -> str:
            return "✅ available" if ok else "❌ NOT found"

        lines = [
            "🔧 *System check*",
            "",
            f"ffmpeg: {check(metadata_mod.ffmpeg_available())}",
            f"ffprobe: {check(metadata_mod.ffprobe_available())}",
            f"mediainfo: {check(metadata_mod.mediainfo_available())}",
            f"yt-dlp: {check(ytdlp.ytdlp_available())}",
            f"aria2c: {check(aria2_available())}",
            f"TORRENT_ENABLED: {'true' if config.TORRENT_ENABLED else 'false'}",
            f"MongoDB: {'✅ enabled (DB_URI set)' if mongo_db.mongo_enabled() else 'disabled (using local JSON file)'}",
        ]
        if not metadata_mod.ffmpeg_available() or not metadata_mod.ffprobe_available():
            lines.append(
                "\n⚠️ ffmpeg/ffprobe missing means: no metadata-title embedding, "
                "no auto-thumbnails/duration on videos, and {languages}/{subtitles} "
                "in captions will always show N/A. If you're on Railway, check that "
                "the service's Builder is set to *Dockerfile*, not Nixpacks -- "
                "Nixpacks skips the `apt-get install ffmpeg` step entirely."
            )
        await message.reply_text("\n".join(lines))

    @app.on_message(filters.command("help"))
    async def help_cmd(client: Client, message: Message):
        torrent_line = (
            "Torrent/magnet support: enabled ✅"
            if torrent_support_enabled()
            else f"Torrent/magnet support: disabled ⚠️ ({torrent_disabled_reason()})"
        )
        owner_line = (
            "\n`/a` — authorize/de-authorize *this* group for /leech (owner-only)\n"
            "`/restart` — restart the bot process (owner-only)\n"
        ) if _is_owner(message.from_user.id if message.from_user else 0) else ""
        await message.reply_text(
            "`/leech <url> [filename]` — download a direct link and upload it here (`/l` shortcut also works)\n"
            "`/leech <url> -e` — download a .zip and upload its extracted contents instead of the zip\n"
            "`/leech <magnet_link>` — download a torrent via magnet and upload its files\n"
            "`/leech <YouTube/other link>` — download via yt-dlp (YouTube, Twitter/X, Instagram, TikTok, Reddit, and more)\n"
            "`/leech <.torrent url>` — same, fetched from a direct .torrent link\n"
            "`/leech` (as a reply to an uploaded `.torrent` file) — same, from that file\n"
            "`/cancel` — cancel your in-progress job\n"
            "`/status` — show progress of your current job\n"
            "`/usetting` (or `/us`) — customize prefix/suffix/caption/thumbnail/metadata/dump/name-swap\n"
            "`/mediainfo` (reply to a video/audio/document) — detailed technical report of that file\n"
            "MediaInfo is also posted automatically after every leeched video/audio file\n"
            "`/stats` — your total files leeched and data downloaded (needs MongoDB)\n"
            f"`/sysinfo` — check if ffmpeg/ffprobe/aria2c/MongoDB are available on this deployment\n{owner_line}\n"
            f"Max file size before splitting: {config.MAX_FILE_SIZE_MB} MB\n"
            f"{torrent_line}\n\n"
            "Pages that require login/JavaScript to reveal the real file URL "
            "still aren't supported for direct links."
        )

    @app.on_message(filters.command("status"))
    async def status_cmd(client: Client, message: Message):
        job = _active_jobs.get(message.from_user.id)
        if not job:
            await message.reply_text("You have no active job.")
            return

        if job.phase == "uploading":
            engine = "Telegram Upload"
        elif isinstance(job, TorrentJob):
            engine = "aria2c (BitTorrent)"
        else:
            engine = "Direct HTTP"
            dl_mode = getattr(job, "download_mode", "")
            if dl_mode and job.phase == "downloading":
                engine = f"{engine} [{dl_mode}]"

        seeders = leechers = None
        if isinstance(job, TorrentJob):
            seeders = job.seeders
            leechers = max(job.connections - job.seeders, 0)

        text = format_status_block(
            _status_label(job), job.downloaded, job.total, job.start_time,
            engine=engine, mode="#Leech", user_mention=_user_mention(message), user_id=job.user_id,
            seeders=seeders, leechers=leechers,
            download_dir=config.DOWNLOAD_DIR, filename=job.current_name, phase=job.phase,
        )
        await message.reply_text(text, reply_markup=_stop_keyboard(job))

    @app.on_callback_query(filters.regex(r"^cancel:(\d+):(\w+)$"))
    async def cancel_callback(client: Client, query: CallbackQuery):
        _, target_id_str, token = query.data.split(":", 2)
        target_id = int(target_id_str)
        if query.from_user.id != target_id and not (config.OWNER_ID and query.from_user.id == config.OWNER_ID):
            await query.answer("This isn't your job.", show_alert=True)
            return
        job = _active_jobs.get(target_id)
        if not job:
            await query.answer("No active job (already finished?).", show_alert=True)
            return
        if job.token != token:
            # This Stop button belongs to an old, already-finished job --
            # never let it cancel whatever the user's current job is.
            await query.answer("This job has already finished.", show_alert=True)
            return
        job.cancelled = True
        await query.answer("🛑 Cancelling...")

    @app.on_message(filters.command("cancel"))
    async def cancel_cmd(client: Client, message: Message):
        # /cancel also aborts an in-progress /usetting prompt (e.g. the bot
        # waiting on a prefix/suffix/thumbnail reply), independently of
        # whether a leech job is running.
        cleared_prompt = pop_pending(message.from_user.id)

        job = _active_jobs.get(message.from_user.id)
        if not job:
            if cleared_prompt:
                await message.reply_text("🛑 Settings input cancelled.")
            else:
                await message.reply_text("You have no active job to cancel.")
            return
        job.cancelled = True
        await message.reply_text("🛑 Cancelling...")

    @app.on_message(filters.command("a"))
    async def authorize_cmd(client: Client, message: Message):
        user_id = message.from_user.id if message.from_user else 0
        if not _is_owner(user_id):
            return  # silent -- don't reveal this command exists to non-owners
        if message.chat.type == ChatType.PRIVATE:
            await message.reply_text("Use `/a` inside a group to authorize/de-authorize it for /leech.")
            return
        now_authorized = await auth_db.toggle(message.chat.id)
        if now_authorized:
            await message.reply_text("✅ This group is now authorized -- /leech works here.")
        else:
            await message.reply_text("🚫 This group is no longer authorized -- /leech is disabled here.")

    @app.on_message(filters.command("restart"))
    async def restart_cmd(client: Client, message: Message):
        if not _is_owner(message.from_user.id if message.from_user else 0):
            return  # silent -- don't reveal this command exists to non-owners

        status = await message.reply_text("🔄 Restarting...")
        try:
            with open(config.RESTART_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"chat_id": status.chat.id, "message_id": status.id}, f)
        except OSError as exc:
            logger.warning("Could not save restart state: %s", exc)

        # Exit and let the host's own process supervisor (Railway, Docker's
        # restart policy, etc.) relaunch the container. os.execl()'ing in
        # place used to be tried here, but replacing the process image
        # mid-coroutine races with asyncio/Pyrogram's own signal handling
        # and in-flight tasks (observed as a "Task attached to a different
        # loop" crash) -- a hard, immediate exit sidesteps all of that, at
        # the cost of relying on the host to actually restart on exit
        # (true for Railway and any standard Docker restart policy).
        logger.info("Restart requested by owner -- exiting for the host to relaunch.")
        os._exit(1)

    @app.on_message(filters.command(["leech", "l"]))
    async def leech_cmd(client: Client, message: Message):
        user_id = message.from_user.id

        access_denied = await _check_leech_access(message)
        if access_denied:
            await message.reply_text(access_denied)
            return

        if not is_allowed(user_id):
            await message.reply_text("⛔ You're not allowed to use this bot.")
            return

        if user_id in _active_jobs:
            await message.reply_text("⏳ You already have a job running. Use /status or /cancel first.")
            return

        raw_tokens = message.text.split()[1:]  # drop the /leech or /l itself
        extract_zip = False
        cleaned_tokens = []
        for tok in raw_tokens:
            if tok.lower() == "-e":
                extract_zip = True
            else:
                cleaned_tokens.append(tok)
        arg = cleaned_tokens[0] if cleaned_tokens else None
        custom_filename = " ".join(cleaned_tokens[1:]) if len(cleaned_tokens) > 1 else None

        torrent_doc = _find_torrent_document(message)

        # ---- Case 1: /leech replying to (or attached with) a .torrent file ----
        if torrent_doc is not None and arg is None:
            if not torrent_support_enabled():
                await message.reply_text(f"⚠️ {torrent_disabled_reason()}")
                return

            target_chat_id = await _resolve_file_target(client, message)
            if target_chat_id is None:
                return

            job = TorrentJob(user_id=user_id, source=torrent_doc.file_name)
            _active_jobs[user_id] = job
            status_msg = await message.reply_text("🔍 Fetching .torrent file...")

            os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
            tmp_torrent_path = os.path.join(
                config.DOWNLOAD_DIR, f"meta_{user_id}_{int(time.time())}.torrent"
            )
            try:
                await client.download_media(torrent_doc.file_id, file_name=tmp_torrent_path)
                await run_torrent_job(client, message, status_msg, job, tmp_torrent_path, target_chat_id,
                                       torrent_file_path=tmp_torrent_path, extract_zip=extract_zip)
            finally:
                cleanup_paths([tmp_torrent_path])
                _active_jobs.pop(user_id, None)
            return

        if arg is None:
            await message.reply_text(
                "Usage: /leech <url or magnet link> [-e] [filename]\n"
                "-e extracts a downloaded .zip archive and uploads its contents instead of the zip itself.\n"
                "Or reply /leech to an uploaded .torrent file."
            )
            return

        # ---- Case 2: magnet link or a direct .torrent URL ----
        if is_torrent_source(arg):
            if not torrent_support_enabled():
                await message.reply_text(f"⚠️ {torrent_disabled_reason()}")
                return

            # A .torrent *URL* is still fetched over the network by aria2 --
            # run it through the same SSRF guard as direct links. Magnet
            # links have no such fetch step (peer/tracker traffic is
            # inherent to the protocol, not an arbitrary server-side fetch).
            if arg.lower().startswith(("http://", "https://")):
                reason = is_safe_url(arg)
                if reason:
                    await message.reply_text(f"⚠️ {reason}")
                    return

            target_chat_id = await _resolve_file_target(client, message)
            if target_chat_id is None:
                return

            job = TorrentJob(user_id=user_id, source=arg)
            _active_jobs[user_id] = job
            status_msg = await message.reply_text("🔍 Connecting...")
            try:
                await run_torrent_job(client, message, status_msg, job, arg, target_chat_id, extract_zip=extract_zip)
            finally:
                _active_jobs.pop(user_id, None)
            return

        # ---- Case 2.5: YouTube or another yt-dlp-supported site ----
        if ytdlp.is_ytdlp_source(arg):
            target_chat_id = await _resolve_file_target(client, message)
            if target_chat_id is None:
                return

            job = Job(user_id=user_id, url=arg)
            _active_jobs[user_id] = job
            status_msg = await message.reply_text("🔍 Connecting...")
            try:
                await run_ytdlp_job(client, message, status_msg, job, arg, custom_filename, target_chat_id, extract_zip=extract_zip)
            finally:
                _active_jobs.pop(user_id, None)
            return

        # ---- Case 3: plain direct HTTP(S) link (existing behavior) ----
        reason = is_safe_url(arg)
        if reason:
            await message.reply_text(f"⚠️ {reason}")
            return

        target_chat_id = await _resolve_file_target(client, message)
        if target_chat_id is None:
            return

        job = Job(user_id=user_id, url=arg)
        _active_jobs[user_id] = job

        status_msg = await message.reply_text("🔍 Connecting...")

        try:
            await run_leech_job(client, message, status_msg, job, arg, custom_filename, target_chat_id, extract_zip=extract_zip)
        finally:
            _active_jobs.pop(user_id, None)


def _find_torrent_document(message: Message) -> Optional[Document]:
    """Returns a .torrent Document attached to this message or the one it
    replies to, if any."""
    for m in (message, message.reply_to_message):
        if m and m.document and (m.document.file_name or "").lower().endswith(".torrent"):
            return m.document
    return None


async def _process_downloaded_file(
    client: Client, message: Message, status_msg: Message, job: "AnyJob",
    target_chat_id: int, dest_path: str, filename: str, extract_zip: bool, download_start: float,
) -> None:
    """Shared post-download pipeline: naming, disguised-extension
    correction, custom metadata, splitting, upload, dump-copy,
    auto-MediaInfo, stats, and the final 'Done!' message. Used by every
    download engine (direct HTTP, yt-dlp) so /usetting behaves identically
    no matter where the file came from."""
    # ---- Apply /usetting naming (name-swap + prefix/suffix) ----
    settings = await settings_db.get_settings(job.user_id)
    filename = apply_naming(filename, settings)
    job.current_name = filename

    # ---- Correct a disguised extension (e.g. a real video served as a
    # misleading ".zip" to dodge filters) so it uploads as native media
    # and its audio/subtitle languages can be read for the caption. ----
    if not metadata_mod.is_media_file(filename):
        corrected_ext = await metadata_mod.detect_real_container(dest_path)
        if corrected_ext:
            filename = os.path.splitext(filename)[0] + corrected_ext

    # ---- Optional custom metadata embed (Global/Video/Audio/Subtitle
    # templates from /usetting -> Metadata, audio/video files only, via
    # ffmpeg) ----
    has_metadata = any(
        settings.get(k) for k in ("metadata_title", "metadata_global", "metadata_video", "metadata_audio", "metadata_subtitle")
    )
    if has_metadata and metadata_mod.is_media_file(filename):
        job.phase = "splitting"  # reuse an existing, harmless status label
        await _safe_edit(status_msg, "🏷 Embedding metadata...")
        dest_path, meta_debug = await metadata_mod.embed_custom_metadata(dest_path, filename, settings)
        if meta_debug and meta_debug.startswith("FAILED"):
            await client.send_message(status_msg.chat.id, f"⚠️ Metadata: ```\n{meta_debug}\n```")

    final_size = os.path.getsize(dest_path)
    await _safe_edit(status_msg, f"✅ Downloaded {human_size(final_size)}. Preparing upload...")

    max_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
    effective_split_mb = settings.get("split_size_mb") or config.SPLIT_SIZE_MB
    split_bytes = effective_split_mb * 1024 * 1024

    # ---- Optional -e: extract a real zip archive before uploading ----
    base_files: List[Tuple[str, str]] = [(dest_path, filename)]
    if extract_zip:
        try:
            extracted = await _extract_zip_if_any(status_msg, dest_path, settings)
        except (zipfile.BadZipFile, OSError) as exc:
            cleanup_paths([dest_path])
            await _safe_edit(status_msg, f"⚠️ Could not extract zip: {exc}")
            return
        if extracted is not None:
            cleanup_paths([dest_path])  # don't also upload the original zip
            if not extracted:
                await _safe_edit(status_msg, "⚠️ Zip archive was empty.")
                return
            base_files = extracted
        # extracted is None -> -e was given but this wasn't really a zip;
        # fall through and upload the downloaded file as-is.

    paths_to_upload: List[str] = []
    part_names: List[str] = []

    for file_path, name in base_files:
        size = os.path.getsize(file_path)

        if size <= max_bytes:
            paths_to_upload.append(file_path)
            part_names.append(name)
            continue

        if effective_split_mb <= 0:
            cleanup_paths([p for p, _ in base_files])
            await _safe_edit(
                status_msg,
                f"⚠️ {name} is {human_size(size)}, which is over the "
                f"{config.MAX_FILE_SIZE_MB} MB limit, and splitting is disabled."
            )
            return

        job.phase = "splitting"
        await _safe_edit(status_msg, f"✂️ Splitting {name} ({human_size(size)})...")
        try:
            parts = split_file(file_path, split_bytes)
        except OSError as exc:
            cleanup_paths([p for p, _ in base_files])
            await _safe_edit(status_msg, f"⚠️ Could not split {name}: {exc}")
            return
        cleanup_paths([file_path])  # original part no longer needed once split
        job.part_paths.extend(parts)
        n = split_path_parts(size, split_bytes)
        for idx, part_path in enumerate(parts):
            paths_to_upload.append(part_path)
            part_names.append(f"{name}.part{idx+1:03d}of{n:03d}")

    # ---- Upload ----
    job.phase = "uploading"
    job.upload_count = len(paths_to_upload)
    asyncio.create_task(_run_status_loop(status_msg, job, {"uploading"}, "Telegram Upload", "#Leech", _user_mention(message)))

    for i, (path, name) in enumerate(zip(paths_to_upload, part_names), start=1):
        if job.cancelled:
            cleanup_paths(paths_to_upload)
            if extract_zip:
                shutil.rmtree(dest_path + "_extracted", ignore_errors=True)
            await _safe_edit(status_msg, "🛑 Cancelled.")
            return

        job.upload_index = i
        job.current_name = name
        job.downloaded = 0
        job.total = os.path.getsize(path)
        job.start_time = time.time()

        def upload_progress(current: int, total: int) -> None:
            job.downloaded = current
            job.total = total

        caption = await _build_media_caption(path, name, settings)
        extras, thumb_path, generated_thumb = await _prepare_video_extras(path, name, settings)
        try:
            sent = await upload_file(
                client, target_chat_id, path, name,
                caption=caption,
                thumb=thumb_path,
                send_as_document=settings.get("send_as_document", False),
                progress=upload_progress,
                **extras,
            )
        except RPCError as exc:
            cleanup_paths(paths_to_upload)
            if extract_zip:
                shutil.rmtree(dest_path + "_extracted", ignore_errors=True)
            await _safe_edit(status_msg, f"⚠️ Upload failed on part {i}/{job.upload_count}: {exc}")
            return
        finally:
            if generated_thumb:
                cleanup_paths([generated_thumb])

        await _copy_to_dump(client, sent, settings.get("dump_chat_id"))
        if job.upload_count == 1:  # skip split parts -- raw byte chunks aren't valid media
            await _auto_mediainfo(client, target_chat_id, path, name)

    cleanup_paths(paths_to_upload)
    if extract_zip:
        shutil.rmtree(dest_path + "_extracted", ignore_errors=True)
    job.phase = "done"
    total_time = time.time() - download_start
    await mongo_db.record_leech(job.user_id, _username(message), final_size)
    pm_note = " -- sent to your PM 📩" if target_chat_id != message.chat.id else ""
    await _safe_edit(
        status_msg,
        f"✅ Done! {human_size(final_size)} in {int(total_time)}s"
        + (f" ({job.upload_count} parts)" if job.upload_count > 1 else "")
        + pm_note
    )


async def run_leech_job(client: Client, message: Message, status_msg: Message, job: Job,
                         url: str, custom_filename: str, target_chat_id: int, extract_zip: bool = False) -> None:
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    tmp_name = f"leech_{job.user_id}_{int(time.time())}.tmp"
    dest_path = os.path.join(config.DOWNLOAD_DIR, tmp_name)

    last_edit = [0.0]

    def on_progress(downloaded: int, total: int) -> None:
        now = time.time()
        if now - last_edit[0] < config.PROGRESS_EDIT_INTERVAL_SECONDS:
            return
        last_edit[0] = now
        job.downloaded = downloaded
        job.total = total

    # ---- Download ----
    job.phase = "downloading"
    job.current_name = custom_filename or filename_from_response(url, None)
    job.start_time = time.time()
    download_start = job.start_time
    try:
        asyncio.create_task(_run_status_loop(status_msg, job, {"downloading"}, "Direct HTTP", "#Leech", _user_mention(message)))
        await download_file(job, url, dest_path, on_progress)
    except Cancelled:
        cleanup_paths([dest_path])
        await _safe_edit(status_msg, "🛑 Cancelled.")
        return
    except DownloadError as exc:
        cleanup_paths([dest_path])
        await _safe_edit(status_msg, f"⚠️ Download failed: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("Download error for %s: %s", url, exc, exc_info=True)
        cleanup_paths([dest_path])
        await _safe_edit(status_msg, f"⚠️ Download failed: {exc}")
        return

    if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
        cleanup_paths([dest_path])
        await _safe_edit(status_msg, "⚠️ Downloaded file is empty or missing.")
        return

    # Belt-and-suspenders: download_file() already checks this internally,
    # but some hosts stall/truncate in ways that slip past a single check
    # point (e.g. no Content-Length at all, or a race with how the
    # connection closes) -- verifying again here, right before anything
    # else touches the file, means a partial file can never reach upload.
    actual_size = os.path.getsize(dest_path)
    if job.total > 0 and actual_size < job.total:
        cleanup_paths([dest_path])
        await _safe_edit(
            status_msg,
            f"⚠️ Incomplete download: got {human_size(actual_size)} of {human_size(job.total)} expected "
            "-- the server cut the connection short. Try again, or use a different link."
        )
        return

    filename = custom_filename or filename_from_response(url, getattr(job, "content_disposition", None))
    await _process_downloaded_file(client, message, status_msg, job, target_chat_id, dest_path, filename, extract_zip, download_start)


async def run_ytdlp_job(client: Client, message: Message, status_msg: Message, job: Job,
                         url: str, custom_filename: str, target_chat_id: int, extract_zip: bool = False) -> None:
    """Handles a YouTube (or any other yt-dlp-supported site) link end to
    end: download via yt-dlp -> the exact same /usetting pipeline
    (naming, metadata, splitting, upload, MediaInfo) as a direct link."""
    dest_dir = config.DOWNLOAD_DIR
    os.makedirs(dest_dir, exist_ok=True)

    job.phase = "downloading"
    job.current_name = custom_filename or url
    job.start_time = time.time()
    download_start = job.start_time

    def on_progress(downloaded: int, total: int) -> None:
        job.downloaded = downloaded
        job.total = total

    try:
        asyncio.create_task(_run_status_loop(status_msg, job, {"downloading"}, "yt-dlp", "#Leech", _user_mention(message)))
        dest_path, title = await ytdlp.download(job, url, dest_dir, on_progress)
    except ytdlp.Cancelled:
        await _safe_edit(status_msg, "🛑 Cancelled.")
        return
    except ytdlp.YtdlpError as exc:
        await _safe_edit(status_msg, f"⚠️ yt-dlp failed: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("yt-dlp error for %s: %s", url, exc, exc_info=True)
        await _safe_edit(status_msg, f"⚠️ yt-dlp failed: {exc}")
        return

    if not dest_path or not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
        if dest_path:
            cleanup_paths([dest_path])
        await _safe_edit(status_msg, "⚠️ Download produced an empty or missing file.")
        return

    filename = custom_filename or (os.path.basename(dest_path))
    await _process_downloaded_file(client, message, status_msg, job, target_chat_id, dest_path, filename, extract_zip, download_start)


async def run_torrent_job(client: Client, message: Message, status_msg: Message, job: TorrentJob,
                           source: str, target_chat_id: int, torrent_file_path: str = None, extract_zip: bool = False) -> None:
    """Handles a magnet link / .torrent URL / .torrent file end to end:
    add to aria2 -> wait for metadata (magnet only) -> poll progress ->
    upload every file the torrent contained, splitting oversized ones
    exactly like the direct-link path does."""
    job.phase = "starting"
    job.current_name = os.path.basename(torrent_file_path) if torrent_file_path else source
    job.start_time = time.time()

    try:
        await add_torrent(job, source, torrent_file_path)
    except TorrentError as exc:
        await _safe_edit(status_msg, f"⚠️ Could not start torrent: {exc}")
        return

    job.phase = "metadata"
    await _safe_edit(status_msg, "🧲 Fetching torrent metadata...")
    try:
        await wait_for_metadata(job)
    except TorrentCancelled:
        await _safe_edit(status_msg, "🛑 Cancelled.")
        return
    except TorrentError as exc:
        await _safe_edit(status_msg, f"⚠️ {exc}")
        return

    async def progress_loop():
        await _run_status_loop(
            status_msg, job, {"metadata", "downloading"},
            "aria2c (BitTorrent)", "#Leech", _user_mention(message),
        )

    asyncio.create_task(progress_loop())

    def on_progress(downloaded: int, total: int) -> None:
        job.downloaded = downloaded
        job.total = total

    try:
        await poll_progress(job, on_progress)
    except TorrentCancelled:
        await _safe_edit(status_msg, "🛑 Cancelled.")
        return
    except TorrentError as exc:
        await _safe_edit(status_msg, f"⚠️ Torrent download failed: {exc}")
        return

    if not job.file_paths:
        await _safe_edit(status_msg, "⚠️ Torrent finished but produced no files.")
        return

    await _safe_edit(
        status_msg,
        f"✅ Torrent downloaded ({len(job.file_paths)} file(s)). Preparing upload..."
    )

    settings = await settings_db.get_settings(job.user_id)
    max_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
    effective_split_mb = settings.get("split_size_mb") or config.SPLIT_SIZE_MB
    split_bytes = effective_split_mb * 1024 * 1024
    excluded_exts = {
        e.strip().lower() for e in (settings.get("excluded_extensions") or "").split(",") if e.strip()
    }

    # ---- Optional -e: extract any real zip archives among the torrent's
    # files before uploading (each extracted entry is then named/split
    # just like any other torrent file below). ----
    files_to_process = job.file_paths
    extract_dirs: List[str] = []
    if extract_zip:
        expanded: List[str] = []
        for file_path in job.file_paths:
            if not os.path.exists(file_path):
                continue
            try:
                extracted = await _extract_zip_if_any(status_msg, file_path, settings)
            except (zipfile.BadZipFile, OSError) as exc:
                await message.reply_text(f"⚠️ Could not extract {os.path.basename(file_path)}: {exc}")
                expanded.append(file_path)
                continue
            if extracted is None:
                expanded.append(file_path)
            else:
                cleanup_paths([file_path])
                extract_dirs.append(file_path + "_extracted")
                expanded.extend(p for p, _ in extracted)
        files_to_process = expanded

    torrent_total_bytes = job.total  # capture before job.total gets reused per-file below
    job.phase = "uploading"
    asyncio.create_task(_run_status_loop(status_msg, job, {"uploading"}, "Telegram Upload", "#Leech", _user_mention(message)))
    upload_plan = []  # list of (path, display_name)
    for file_path in files_to_process:
        if not os.path.exists(file_path):
            continue

        name = apply_naming(os.path.basename(file_path), settings)

        if os.path.splitext(name)[1].lower() in excluded_exts:
            cleanup_paths([file_path])
            continue

        # Correct a disguised extension (e.g. a real video served as a
        # misleading ".zip") so it uploads as native media and its
        # audio/subtitle languages can be read for the caption.
        if not metadata_mod.is_media_file(name):
            corrected_ext = await metadata_mod.detect_real_container(file_path)
            if corrected_ext:
                name = os.path.splitext(name)[0] + corrected_ext

        # Optional custom metadata embed (Global/Video/Audio/Subtitle
        # templates, audio/video only, via ffmpeg) -- done before the
        # size check since a remux can change the size.
        has_metadata = any(
            settings.get(k) for k in ("metadata_title", "metadata_global", "metadata_video", "metadata_audio", "metadata_subtitle")
        )
        if has_metadata and metadata_mod.is_media_file(name):
            file_path, meta_debug = await metadata_mod.embed_custom_metadata(file_path, name, settings)
            if meta_debug and meta_debug.startswith("FAILED"):
                await client.send_message(status_msg.chat.id, f"⚠️ Metadata for {name}: ```\n{meta_debug}\n```")

        size = os.path.getsize(file_path)

        if size <= max_bytes:
            upload_plan.append((file_path, name))
            continue

        if effective_split_mb <= 0:
            await message.reply_text(
                f"⚠️ Skipping *{name}*: {human_size(size)} is over the "
                f"{config.MAX_FILE_SIZE_MB} MB limit and splitting is disabled."
            )
            cleanup_paths([file_path])
            continue

        try:
            parts = split_file(file_path, split_bytes)
        except OSError as exc:
            await message.reply_text(f"⚠️ Could not split *{name}*: {exc}")
            cleanup_paths([file_path])
            continue
        cleanup_paths([file_path])
        n = split_path_parts(size, split_bytes)
        for idx, p in enumerate(parts):
            upload_plan.append((p, f"{name}.part{idx + 1:03d}of{n:03d}"))

    if not upload_plan:
        for d in extract_dirs:
            shutil.rmtree(d, ignore_errors=True)
        await _safe_edit(status_msg, "⚠️ Nothing left to upload after filtering oversized files.")
        return

    job.upload_count = len(upload_plan)
    for i, (path, name) in enumerate(upload_plan, start=1):
        if job.cancelled:
            cleanup_paths([p for p, _ in upload_plan])
            for d in extract_dirs:
                shutil.rmtree(d, ignore_errors=True)
            await _safe_edit(status_msg, "🛑 Cancelled.")
            return

        job.upload_index = i
        job.current_name = name
        job.downloaded = 0
        job.total = os.path.getsize(path)
        job.start_time = time.time()

        def upload_progress(current: int, total: int) -> None:
            job.downloaded = current
            job.total = total

        caption = await _build_media_caption(path, name, settings)
        extras, thumb_path, generated_thumb = await _prepare_video_extras(path, name, settings)
        try:
            sent = await upload_file(
                client, target_chat_id, path, name,
                caption=caption,
                thumb=thumb_path,
                send_as_document=settings.get("send_as_document", False),
                progress=upload_progress,
                **extras,
            )
            await _copy_to_dump(client, sent, settings.get("dump_chat_id"))
            if not re.search(r"\.part\d{3}of\d{3}$", name):  # skip split parts
                await _auto_mediainfo(client, target_chat_id, path, name)
        except RPCError as exc:
            await _safe_edit(status_msg, f"⚠️ Upload failed on {name}: {exc}")
        finally:
            cleanup_paths([path])
            if generated_thumb:
                cleanup_paths([generated_thumb])

    for d in extract_dirs:
        shutil.rmtree(d, ignore_errors=True)

    job.phase = "done"
    await mongo_db.record_leech(job.user_id, _username(message), torrent_total_bytes)
    pm_note = " -- sent to your PM 📩" if target_chat_id != message.chat.id else ""
    await _safe_edit(status_msg, f"✅ Done! Uploaded {job.upload_count} file(s) from the torrent.{pm_note}")
