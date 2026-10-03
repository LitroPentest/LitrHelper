"""Мелкие утилиты: разбор ссылок, форматирование, обрезка текста."""

from __future__ import annotations

import re
from urllib.parse import urlparse

URL_RE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)+[a-zA-Zа-яА-Я]{2,}(?:/[^\s<>\"']*)?",
    re.IGNORECASE | re.UNICODE,
)

TIKTOK_HOSTS = ("tiktok.com",)
SOUNDCLOUD_HOSTS = ("soundcloud.com", "snd.sc", "on.soundcloud.com")


def extract_url(text: str) -> str | None:
    """Достать первую похожую на ссылку строку из запроса."""
    for match in URL_RE.finditer(text or ""):
        candidate = match.group(0).strip().rstrip('.,;:!?)"\'»')
        if not candidate.lower().startswith(("http://", "https://")):
            candidate = "https://" + candidate
        host = (urlparse(candidate).hostname or "").lower()
        if "." in host and len(host) > 4:
            return candidate
    return None


def classify_url(url: str) -> str | None:
    """Вернуть 'tiktok' / 'soundcloud' / None для ссылки."""
    host = (urlparse(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if any(host == h or host.endswith("." + h) or host.endswith(h) for h in TIKTOK_HOSTS):
        return "tiktok"
    if any(h in host for h in SOUNDCLOUD_HOSTS):
        return "soundcloud"
    return None


def truncate(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def format_duration(seconds: float | int | None) -> str:
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "—"
    if total <= 0:
        return "—"
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def human_size(num_bytes: float | int | None) -> str:
    try:
        size = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def split_track_query(text: str) -> tuple[str, str | None]:
    """'Артист - Песня' -> ('Артист', 'Песня')."""
    for sep in (" - ", " – ", " — ", "-", "–", "—"):
        if sep in text:
            left, _, right = text.partition(sep)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return text.strip(), None


VIDEO_EXT = ("mp4", "mov", "m4v", "webm", "mkv", "avi", "3gp")
AUDIO_EXT = ("mp3", "m4a", "aac", "ogg", "oga", "opus", "flac", "wav")


def guess_media_type(name: str | None) -> str | None:
    """Определить тип медиа по расширению файла или URL.

    Нужно, потому что TikTok отдаёт аудио-посты как mp3, а SoundCloud — mp3/m4a:
    Telegram отвергнет ссылку, если назвать аудио видео.
    """
    if not name:
        return None
    path = urlparse(str(name)).path
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if suffix in VIDEO_EXT:
        return "video"
    if suffix in AUDIO_EXT:
        return "audio"
    return None