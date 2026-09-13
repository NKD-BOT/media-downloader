"""
utils.py
Small helpers: human-readable byte/speed formatting, a text progress bar,
and figuring out a safe filename from a URL / Content-Disposition header.
"""

import os
import re
import time
import unicodedata
from urllib.parse import urlparse, unquote
from typing import Optional


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def human_speed(bytes_per_second: float) -> str:
    return f"{human_size(bytes_per_second)}/s"


def human_eta(seconds: float) -> str:
    if seconds <= 0 or seconds == float("inf"):
        return "—"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def progress_bar(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "[" + "○" * width + "]"
    filled = int(width * done / total)
    filled = max(0, min(width, filled))
    return "[" + "●" * filled + "○" * (width - filled) + "]"


def format_progress(label: str, done: int, total: int, start_time: float) -> str:
    elapsed = max(time.time() - start_time, 0.001)
    speed = done / elapsed
    pct = (done / total * 100) if total > 0 else 0
    eta = (total - done) / speed if speed > 0 and total > 0 else float("inf")
    bar = progress_bar(done, total)
    total_str = human_size(total) if total > 0 else "?"
    return (
        f"{label}\n"
        f"{bar} {pct:.1f}%\n"
        f"{human_size(done)} / {total_str}\n"
        f"Speed: {human_speed(speed)} — ETA: {human_eta(eta)}"
    )


def format_status_block(
    label: str,
    done: int,
    total: int,
    start_time: float,
    engine: str,
    mode: str,
    user_mention: str,
    user_id: int,
    seeders: Optional[int] = None,
    leechers: Optional[int] = None,
) -> str:
    """Builds the rich, card-style status block used for live /leech
    progress messages: a circle progress bar plus percentage, processed
    size, speed, ETA, and (for torrents) seeder/leecher counts, engine,
    upload mode, and who started the job."""
    elapsed = max(time.time() - start_time, 0.001)
    speed = done / elapsed
    pct = (done / total * 100) if total > 0 else 0
    eta = (total - done) / speed if speed > 0 and total > 0 else float("inf")
    bar = progress_bar(done, total)
    total_str = human_size(total) if total > 0 else "?"

    lines = [
        f"✨ {label}...",
        bar,
        f"📊 Process   : {pct:.2f}%",
        f"Processed : {human_size(done)} of {total_str}",
        f"✈️ Speed     : {human_speed(speed)}",
        f"⏱️ Time      : {human_eta(eta)} (elapsed {human_eta(elapsed)})",
    ]
    if seeders is not None and leechers is not None:
        lines.append(f"Seeders   : {seeders} | Leechers : {leechers}")
    lines.append(f"🚗 Engine    : {engine}")
    lines.append(f"🤖 Mode      : {mode}")
    lines.append(f"😊 User/ID   : {user_mention} | {user_id}")
    return "\n".join(lines)


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r"[^\w\-. ]", "_", name).strip()
    return name[:200] if name else "file"


def filename_from_response(url: str, content_disposition: Optional[str]) -> str:
    """Best-effort filename resolution: Content-Disposition header first,
    then the URL path, then a generic fallback."""
    if content_disposition:
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";\n]+)", content_disposition, re.IGNORECASE)
        if match:
            return safe_filename(unquote(match.group(1)))

    path = urlparse(url).path
    base = os.path.basename(unquote(path))
    if base:
        return safe_filename(base)

    return "downloaded_file"


def split_path_parts(total_size: int, part_size: int) -> int:
    """Number of parts a file of total_size bytes will be split into at
    part_size bytes each."""
    if part_size <= 0:
        return 1
    return max(1, (total_size + part_size - 1) // part_size)


def apply_naming(filename: str, settings: dict) -> str:
    """Applies a user's /usetting name-swap patterns (if enabled), then
    their prefix/suffix, to a filename -- extension is preserved
    untouched throughout.

    Name-swap patterns are regular expressions (re.sub, case-insensitive),
    not plain substrings -- a plain site name like "Vegamovies" still
    matches exactly like a literal replace would, but this also lets a
    pattern like a full URL regex strip arbitrarily-varying text (which a
    literal find/replace could never do). An empty replacement removes
    the match entirely. Invalid regexes are skipped rather than crashing
    the leech. After all patterns are applied, repeated separators left
    behind by removed words (e.g. "Movie..2023..mkv") are collapsed."""
    base, ext = os.path.splitext(filename)

    if settings.get("name_swap_enabled"):
        for pair in settings.get("name_swap_pairs", []):
            if len(pair) == 2 and pair[0]:
                try:
                    base = re.sub(pair[0], pair[1], base, flags=re.IGNORECASE)
                except re.error:
                    continue  # invalid regex -- skip rather than break the leech
        # Tidy up leftover separator runs (".. ", "--", etc.) from removed words
        base = re.sub(r"[.\-_ ]{2,}", ".", base)
        base = base.strip(" .-_")

    prefix = settings.get("leech_prefix") or ""
    suffix = settings.get("leech_suffix") or ""
    base = f"{prefix}{base}{suffix}"

    return safe_filename(base) + ext


def build_caption(
    filename: str,
    size_bytes: int,
    template: str,
    languages: Optional[list] = None,
    subtitles: Optional[list] = None,
) -> str:
    """Builds an upload caption from a user's /usetting caption template.
    Supports {filename}, {basename}, {ext}, {size}, {languages}, and
    {subtitles} placeholders; falls back to the plain filename if no
    template is set or the template is malformed. HTML tags in the
    template (e.g. <b>, <a href=...>, <pre>) render normally since
    Pyrogram parses HTML in captions by default.

    Telegram caps media captions at 1024 characters, so the result is
    truncated to that length as a safety net.
    """
    if not template:
        return filename
    base, ext = os.path.splitext(filename)
    lang_str = ", ".join(languages) if languages else "N/A"
    subs_str = ", ".join(subtitles) if subtitles else "N/A"
    try:
        caption = template.format(
            filename=filename,
            basename=base,
            ext=ext.lstrip("."),
            size=human_size(size_bytes),
            languages=lang_str,
            subtitles=subs_str,
        )
    except (KeyError, IndexError, ValueError):
        return filename
    return caption[:1024]
