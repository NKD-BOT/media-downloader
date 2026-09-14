"""
leech_settings.py
The /usetting inline-button menu (mirrors the classic "Leech Settings"
panel: Send As Document, Thumbnail, Leech Prefix/Suffix/Caption,
Metadata, Dump, Name Swap). Each per-user setting is persisted via
settings_db.py and applied at upload time in handlers.py.

Text-value settings (prefix, suffix, caption, metadata title, dump chat,
name-swap pairs) work as a tiny two-step conversation: tapping the button
edits the menu into a prompt and remembers that this user is "awaiting"
that field; their next plain-text message in the same chat fills it in
and the menu is redrawn. /cancel (or the Close button) aborts an
in-progress prompt.
"""

import asyncio
import logging
import os
import re
from typing import Any, Dict, Optional

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
import settings_db

logger = logging.getLogger("leech.settings_menu")

THUMB_DIR = os.path.join(config.DOWNLOAD_DIR, "thumbnails")

# user_id -> {"key": <setting awaiting input>, "chat_id": int, "message_id": int}
_pending: Dict[int, Dict[str, Any]] = {}

_PROMPTS = {
    "leech_prefix": "Send the new *leech prefix* (added before the filename), or `-` to clear it.",
    "leech_suffix": "Send the new *leech suffix* (added after the filename, before the extension), or `-` to clear it.",
    "leech_caption": (
        "Send the new *leech caption*. HTML tags work (`<b>`, `<a href='...'>`, `<pre>`, etc.), and "
        "these placeholders are supported: `{filename}` `{basename}` `{ext}` `{size}` `{languages}` `{subtitles}`.\n"
        "Example: `<b><a href='https://t.me/yourchannel'>{filename}</a></b>`\n"
        "Send `-` to clear it."
    ),
    "metadata_global": (
        "Send *Global metadata* -- applies once to the whole file. Format: `key=value`, "
        "several separated by commas. Supports `{basename}`/`{filename}`.\n"
        "Example: `Auth=@CARZYHUBXBOT, title={basename}`\n"
        "Send `-` to clear it."
    ),
    "metadata_video": (
        "Send *Video stream metadata* -- applied to every video stream. Format: `key=value`, "
        "several separated by commas.\n"
        "Example: `title=@CARZYHUBXBOT`\n"
        "Send `-` to clear it."
    ),
    "metadata_audio": (
        "Send *Audio stream metadata* -- applied to every audio stream. Format: `key=value`, "
        "several separated by commas. `{audiolang}` = raw code (e.g. `hin`, correct for the "
        "`language` field itself); `{audiolang_name}` = readable name (e.g. `Hindi`, for use in a title).\n"
        "Example: `language={audiolang}, title=@CARZYHUBXBOT - {audiolang_name}`\n"
        "Send `-` to clear it."
    ),
    "metadata_subtitle": (
        "Send *Subtitle stream metadata* -- applied to every subtitle stream. Format: `key=value`, "
        "several separated by commas. `{sublang}` = raw code (e.g. `eng`, correct for the "
        "`language` field itself); `{sublang_name}` = readable name (e.g. `English`, for use in a title).\n"
        "Example: `language={sublang}, title=@CARZYHUBXBOT - {sublang_name}`\n"
        "Send `-` to clear it."
    ),
    "dump_chat_id": "Send the *dump chat ID* every leeched file should also be copied to (the bot must already be a member/admin there), or `-` to clear it.",
    "name_swap_pair": (
        "Send one or more patterns to strip/replace, as `find:::replace`. "
        "Add several at once by separating them with `|`.\n"
        "Leave the replacement empty to just remove a match, e.g. `Vegamovies:::`.\n"
        "Patterns are regular expressions (case-insensitive), so this also works: "
        "`(www\\.[^\\s/$.?#].[^\\s]*):::`\n"
        "Example: `Vegamovies:::|ExtraFlix:::|4kHdHub:::|(www\\.[^\\s/$.?#].[^\\s]*):::`"
    ),
    "thumbnail": "Send a *photo* to use as the new thumbnail, or send `-` to remove the current one.",
}


def pop_pending(user_id: int) -> bool:
    """Clears any in-progress settings prompt for this user. Returns True
    if there was one (used by /cancel in handlers.py)."""
    return _pending.pop(user_id, None) is not None


async def _delete_quietly(message: Message) -> None:
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass


async def _confirm_and_cleanup(client: Client, message: Message, text: str, delay: float = 2.0) -> None:
    """Deletes the user's input message right away, briefly shows a
    confirmation, then deletes that too -- so saving a setting doesn't
    leave clutter behind once the menu itself reflects the new value."""
    await _delete_quietly(message)
    try:
        confirm = await client.send_message(message.chat.id, text)
    except Exception:  # noqa: BLE001
        return
    await asyncio.sleep(delay)
    await _delete_quietly(confirm)


def _doc_toggle_label(send_as_document: bool) -> str:
    return "✅ Send As Document" if send_as_document else "🎬 Send As Media"


def _main_menu_text(settings: Dict[str, Any]) -> str:
    lines = ["⚙️ *Leech Settings*", ""]
    lines.append(f"Upload mode: {'Document' if settings['send_as_document'] else 'Media'}")
    lines.append(f"Prefix: `{settings['leech_prefix'] or '—'}`")
    lines.append(f"Suffix: `{settings['leech_suffix'] or '—'}`")
    lines.append(f"Caption: `{settings['leech_caption'] or '—'}`")
    metadata_set = any(settings.get(k) for k in ("metadata_title", "metadata_global", "metadata_video", "metadata_audio", "metadata_subtitle"))
    lines.append(f"Metadata: {'set ✅' if metadata_set else 'not set'}")
    lines.append(f"Dump chat: `{settings['dump_chat_id'] or '—'}`")
    lines.append(f"Thumbnail: {'set ✅' if settings['thumbnail_path'] else 'not set'}")
    swap_state = "on" if settings["name_swap_enabled"] else "off"
    lines.append(f"Name swap: {swap_state} ({len(settings['name_swap_pairs'])} pair(s))")
    return "\n".join(lines)


def _main_menu_keyboard(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(_doc_toggle_label(settings["send_as_document"]), callback_data="lset:toggle_doc"),
            InlineKeyboardButton("🖼 Thumbnail", callback_data="lset:thumb"),
        ],
        [
            InlineKeyboardButton("✏️ Leech Prefix", callback_data="lset:prefix"),
            InlineKeyboardButton("✏️ Leech Suffix", callback_data="lset:suffix"),
        ],
        [
            InlineKeyboardButton("📝 Leech Caption", callback_data="lset:caption"),
            InlineKeyboardButton("🏷 Metadata", callback_data="lset:meta_menu"),
        ],
        [
            InlineKeyboardButton("📤 Dump", callback_data="lset:dump"),
            InlineKeyboardButton("🔀 Name Swap", callback_data="lset:swap_menu"),
        ],
        [
            InlineKeyboardButton("❌ Close", callback_data="lset:close"),
        ],
    ])


def _metadata_menu_text(settings: Dict[str, Any]) -> str:
    lines = ["🏷 *Metadata*", "", "Applied via ffmpeg before upload (video/audio files only).", ""]
    lines.append(f"Global: `{settings.get('metadata_global') or '—'}`")
    lines.append(f"Video: `{settings.get('metadata_video') or '—'}`")
    lines.append(f"Audio: `{settings.get('metadata_audio') or '—'}`")
    lines.append(f"Subtitle: `{settings.get('metadata_subtitle') or '—'}`")
    if settings.get("metadata_title"):
        lines.append(f"\n_Legacy title (still applied if the fields above are empty): `{settings['metadata_title']}`_")
    return "\n".join(lines)


def _metadata_menu_keyboard(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Global", callback_data="lset:meta_global")],
        [InlineKeyboardButton("🎬 Video", callback_data="lset:meta_video")],
        [InlineKeyboardButton("🔊 Audio", callback_data="lset:meta_audio")],
        [InlineKeyboardButton("📝 Subtitle", callback_data="lset:meta_subtitle")],
        [InlineKeyboardButton("🗑 Clear All", callback_data="lset:meta_clear")],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="lset:back"),
            InlineKeyboardButton("❌ Close", callback_data="lset:close"),
        ],
    ])


def _swap_menu_text(settings: Dict[str, Any]) -> str:
    pairs = settings_db.get_name_swap_pairs(settings)
    lines = ["🔀 *Name Swap*", "", f"Status: {'enabled ✅' if settings['name_swap_enabled'] else 'disabled ⬜'}", ""]
    if pairs:
        lines.append("Pairs:")
        lines.extend(f"• `{f}` → `{r}`" for f, r in pairs)
    else:
        lines.append("No pairs configured yet.")
    return "\n".join(lines)


def _swap_menu_keyboard(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    toggle_label = "⬜ Disable" if settings["name_swap_enabled"] else "✅ Enable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label, callback_data="lset:swap_toggle")],
        [InlineKeyboardButton("➕ Add Pair", callback_data="lset:swap_add")],
        [InlineKeyboardButton("🗑 Clear Pairs", callback_data="lset:swap_clear")],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="lset:back"),
            InlineKeyboardButton("❌ Close", callback_data="lset:close"),
        ],
    ])


async def _render_main(message: Message, settings: Dict[str, Any]) -> None:
    await message.edit_text(
        _main_menu_text(settings),
        reply_markup=_main_menu_keyboard(settings),
    )


async def _render_swap(message: Message, settings: Dict[str, Any]) -> None:
    await message.edit_text(
        _swap_menu_text(settings),
        reply_markup=_swap_menu_keyboard(settings),
    )


async def _render_metadata(message: Message, settings: Dict[str, Any]) -> None:
    await message.edit_text(
        _metadata_menu_text(settings),
        reply_markup=_metadata_menu_keyboard(settings),
    )


def _prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="lset:back")]])


async def _ask_for(query: CallbackQuery, key: str, menu: str = "main") -> None:
    _pending[query.from_user.id] = {
        "key": key,
        "chat_id": query.message.chat.id,
        "message_id": query.message.id,
        "menu": menu,  # which menu to redraw once this field is saved
    }
    await query.message.edit_text(_PROMPTS[key], reply_markup=_prompt_keyboard())


def register_settings_handlers(app: Client) -> None:

    @app.on_message(filters.command(["usetting", "us"]))
    async def leechset_cmd(client: Client, message: Message):
        settings = await settings_db.get_settings(message.from_user.id)
        await message.reply_text(
            _main_menu_text(settings),
            reply_markup=_main_menu_keyboard(settings),
        )

    @app.on_callback_query(filters.regex(r"^lset:"))
    async def leechset_callback(client: Client, query: CallbackQuery):
        user_id = query.from_user.id
        action = query.data.split(":", 1)[1]
        settings = await settings_db.get_settings(user_id)

        if action == "close":
            _pending.pop(user_id, None)
            await query.message.edit_text("Settings closed.")
            await query.answer()
            return

        if action == "back":
            pending = _pending.pop(user_id, None)
            settings = await settings_db.get_settings(user_id)
            return_menu = pending.get("menu") if pending else None
            if return_menu == "metadata":
                await _render_metadata(query.message, settings)
            elif return_menu == "swap":
                await _render_swap(query.message, settings)
            else:
                await _render_main(query.message, settings)
            await query.answer()
            return

        if action == "toggle_doc":
            settings = await settings_db.update_settings(user_id, send_as_document=not settings["send_as_document"])
            await _render_main(query.message, settings)
            await query.answer()
            return

        if action == "thumb":
            await _ask_for(query, "thumbnail")
            await query.answer()
            return

        if action == "prefix":
            await _ask_for(query, "leech_prefix")
            await query.answer()
            return

        if action == "suffix":
            await _ask_for(query, "leech_suffix")
            await query.answer()
            return

        if action == "caption":
            await _ask_for(query, "leech_caption")
            await query.answer()
            return

        if action == "metadata":
            await _ask_for(query, "metadata_title")
            await query.answer()
            return

        if action == "meta_menu":
            await _render_metadata(query.message, settings)
            await query.answer()
            return

        if action == "meta_global":
            await _ask_for(query, "metadata_global", menu="metadata")
            await query.answer()
            return

        if action == "meta_video":
            await _ask_for(query, "metadata_video", menu="metadata")
            await query.answer()
            return

        if action == "meta_audio":
            await _ask_for(query, "metadata_audio", menu="metadata")
            await query.answer()
            return

        if action == "meta_subtitle":
            await _ask_for(query, "metadata_subtitle", menu="metadata")
            await query.answer()
            return

        if action == "meta_clear":
            settings = await settings_db.update_settings(
                user_id, metadata_title=None, metadata_global="", metadata_video="",
                metadata_audio="", metadata_subtitle="",
            )
            await _render_metadata(query.message, settings)
            await query.answer("Cleared all metadata.")
            return

        if action == "dump":
            await _ask_for(query, "dump_chat_id")
            await query.answer()
            return

        if action == "swap_menu":
            await _render_swap(query.message, settings)
            await query.answer()
            return

        if action == "swap_toggle":
            settings = await settings_db.update_settings(user_id, name_swap_enabled=not settings["name_swap_enabled"])
            await _render_swap(query.message, settings)
            await query.answer()
            return

        if action == "swap_add":
            await _ask_for(query, "name_swap_pair", menu="swap")
            await query.answer()
            return

        if action == "swap_clear":
            settings = await settings_db.clear_name_swap_pairs(user_id)
            await _render_swap(query.message, settings)
            await query.answer("Cleared all pairs.")
            return

        await query.answer()

    @app.on_message(filters.photo & filters.private)
    async def thumbnail_capture(client: Client, message: Message):
        pending = _pending.get(message.from_user.id)
        if not pending or pending["key"] != "thumbnail" or pending["chat_id"] != message.chat.id:
            return  # not something we're waiting on -- let other handlers ignore this too

        os.makedirs(THUMB_DIR, exist_ok=True)
        dest = os.path.join(THUMB_DIR, f"{message.from_user.id}.jpg")
        try:
            await client.download_media(message, file_name=dest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Thumbnail download failed for %s: %s", message.from_user.id, exc)
            await message.reply_text("⚠️ Could not save that photo as a thumbnail.")
            return

        _pending.pop(message.from_user.id, None)
        settings = await settings_db.update_settings(message.from_user.id, thumbnail_path=dest)
        try:
            await client.edit_message_text(
                pending["chat_id"], pending["message_id"],
                _main_menu_text(settings), reply_markup=_main_menu_keyboard(settings),
            )
        except Exception:  # noqa: BLE001
            pass
        await _confirm_and_cleanup(client, message, "✅ Thumbnail saved.")

    @app.on_message(filters.text & filters.private & ~filters.command(["cancel", "usetting", "us", "leech", "l", "ytdl", "status", "help", "start", "mediainfo", "sysinfo", "stats", "a"]))
    async def text_input_capture(client: Client, message: Message):
        pending = _pending.get(message.from_user.id)
        if not pending or pending["chat_id"] != message.chat.id:
            return

        key = pending["key"]
        value = message.text.strip()

        if key == "thumbnail":
            if value == "-":
                path = os.path.join(THUMB_DIR, f"{message.from_user.id}.jpg")
                if os.path.exists(path):
                    os.remove(path)
                settings = await settings_db.update_settings(message.from_user.id, thumbnail_path=None)
                _pending.pop(message.from_user.id, None)
                await _finish(client, message, pending, settings)
            else:
                await message.reply_text("Please send a *photo*, or `-` to remove the thumbnail.")
            return

        if key == "name_swap_pair":
            entries = [e for e in value.split("|") if e.strip()]
            valid: list = []
            skipped = 0
            for entry in entries:
                parts = re.split(r":{2,}", entry, maxsplit=1)  # tolerate "::" or ":::"
                if len(parts) != 2:
                    skipped += 1
                    continue
                find, replace = parts
                find = find.strip()
                if not find:
                    skipped += 1
                    continue
                try:
                    re.compile(find)
                except re.error:
                    skipped += 1
                    continue
                valid.append((find, replace.strip()))

            if not valid:
                await message.reply_text(
                    "No valid patterns found. Format is `find:::replace` "
                    "(separate several with `|`), and `find` must be a valid regex."
                )
                return

            settings = await settings_db.add_name_swap_pairs(message.from_user.id, valid)
            if not settings.get("name_swap_enabled"):
                settings = await settings_db.update_settings(message.from_user.id, name_swap_enabled=True)
            _pending.pop(message.from_user.id, None)
            try:
                await client.edit_message_text(
                    pending["chat_id"], pending["message_id"],
                    _swap_menu_text(settings), reply_markup=_swap_menu_keyboard(settings),
                )
            except Exception:  # noqa: BLE001
                pass
            confirm_text = f"✅ Added {len(valid)} pair(s), name swap turned on."
            if skipped:
                confirm_text += f" Skipped {skipped} invalid entrie(s)."
            await _confirm_and_cleanup(client, message, confirm_text)
            return

        if key == "dump_chat_id":
            if value == "-":
                settings = await settings_db.update_settings(message.from_user.id, dump_chat_id=None)
            else:
                try:
                    settings = await settings_db.update_settings(message.from_user.id, dump_chat_id=int(value))
                except ValueError:
                    await message.reply_text("Dump chat ID must be a number (e.g. -1001234567890), or `-` to clear it.")
                    return
            _pending.pop(message.from_user.id, None)
            await _finish(client, message, pending, settings)
            return

        # Simple text fields: leech_prefix / leech_suffix / leech_caption /
        # metadata_title / metadata_global / metadata_video / metadata_audio / metadata_subtitle
        new_value = None if value == "-" else value
        settings = await settings_db.update_settings(message.from_user.id, **{key: new_value})
        _pending.pop(message.from_user.id, None)
        await _finish(client, message, pending, settings)


async def _finish(client: Client, message: Message, pending: Dict[str, Any], settings: Dict[str, Any]) -> None:
    menu = pending.get("menu", "main")
    text, markup = (
        (_metadata_menu_text(settings), _metadata_menu_keyboard(settings))
        if menu == "metadata"
        else (_main_menu_text(settings), _main_menu_keyboard(settings))
    )
    try:
        await client.edit_message_text(pending["chat_id"], pending["message_id"], text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        pass
    await _confirm_and_cleanup(client, message, "✅ Saved.")
