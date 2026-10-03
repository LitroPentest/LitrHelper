"""SoundCloud: поиск треков по API и скачивание аудио.

Поиск идёт через официальный api-v2 (client_id берётся со страницы soundcloud.com).
Если API недоступен — фолбэк: парсим ссылки со страницы поиска и добираем
названия через oEmbed (oEmbed работает вообще без client_id).

Скачивание делает yt-dlp, запущенный ОТДЕЛЬНЫМ процессом:
- бот не блокируется на время загрузки;
- память yt-dlp не живёт постоянно в процессе бота.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse
from uuid import uuid4

from config import CONVERT_TO_MP3, DOWNLOAD_TIMEOUT, FFMPEG_PATH, MAX_UPLOAD_BYTES, TMP_DIR
from src.helpers import human_size
from src.http_client import http

log = logging.getLogger("bot.soundcloud")

API_SEARCH = "https://api-v2.soundcloud.com/search/tracks"
OEMBED = "https://soundcloud.com/oembed"
CLIENT_ID_TTL = 3600.0

_client_id: str | None = None
_client_id_time: float = 0.0


# --------------------------------------------------------------------------- #
# client_id
# --------------------------------------------------------------------------- #
# SoundCloud отдаёт рабочий client_id прямо в HTML главной страницы:
#   {"hydratable":"apiClient","data":{"id":"XXXX","isExpiring":false}}
CLIENT_ID_PATTERNS = (
    r'"hydratable":"apiClient","data":\{"id":"([A-Za-z0-9_-]{16,})"',
    r'"apiClient".{0,40}?"id":"([A-Za-z0-9_-]{16,})"',
    r'"client_id"\s*:\s*"([A-Za-z0-9_-]{16,})"',
)
CLIENT_ID_PROBE = "https://api-v2.soundcloud.com/search/tracks"
DISCOVERY_PAGES = ("https://soundcloud.com/", "https://m.soundcloud.com/")


async def get_client_id() -> str | None:
    global _client_id, _client_id_time
    if _client_id and time.monotonic() - _client_id_time < CLIENT_ID_TTL:
        return _client_id

    session = await http.session()
    candidates: list[str] = []

    for page in DISCOVERY_PAGES:
        try:
            async with session.get(page, timeout=http.timeout(20)) as resp:
                html = await resp.text(errors="ignore")
        except Exception as exc:  # noqa: BLE001
            log.warning("не открылась %s: %s", page, exc)
            continue

        for pattern in CLIENT_ID_PATTERNS:
            match = re.search(pattern, html)
            if match:
                candidates.append(match.group(1))
        # запасные кандидаты: 32-символьные токены, проверим их запросом к API
        candidates += re.findall(r"[A-Za-z0-9]{32}", html)

    for candidate in dict.fromkeys(candidates):  # уникальные, в порядке появления
        if await _client_id_works(session, candidate):
            _client_id, _client_id_time = candidate, time.monotonic()
            log.info("SoundCloud client_id получен")
            return _client_id

    log.warning("SoundCloud client_id не найден — включён резервный поиск")
    return _client_id


async def _client_id_works(session, client_id: str) -> bool:
    try:
        async with session.get(
            CLIENT_ID_PROBE, params={"client_id": client_id, "q": "test", "limit": 1}
        ) as resp:
            if resp.status != 200:
                return False
            data = await resp.json(content_type=None)
        return isinstance(data, dict) and "collection" in data
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# поиск
# --------------------------------------------------------------------------- #
async def search(query: str, limit: int = 8) -> list[dict]:
    client_id = await get_client_id()
    if client_id:
        tracks = await _search_api(query, limit, client_id)
        if tracks:
            return tracks
    return await _search_scrape(query, limit)


def _duration_seconds(raw: object) -> int:
    """SoundCloud местами отдаёт длительность в миллисекундах."""
    try:
        value = int(float(raw or 0))
    except (TypeError, ValueError):
        return 0
    return value // 1000 if value > 3600 else value


async def _search_api(query: str, limit: int, client_id: str) -> list[dict]:
    session = await http.session()
    params = {"client_id": client_id, "q": query, "limit": min(limit, 20), "offset": 0}
    try:
        async with session.get(API_SEARCH, params=params) as resp:
            if resp.status != 200:
                log.warning("SoundCloud search вернул HTTP %s", resp.status)
                return []
            data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("SoundCloud search: %s", exc)
        return []

    results: list[dict] = []
    for item in (data or {}).get("collection") or []:
        permalink = item.get("permalink_url")
        if not permalink:
            continue
        user = item.get("user") or {}
        cover = (item.get("artwork_url") or "").replace("-large", "-t200x200")
        results.append(
            {
                "url": permalink,
                "title": item.get("title") or "Без названия",
                "author": user.get("username") or "",
                "duration": _duration_seconds(item.get("duration")),
                "cover": cover,
            }
        )
        if len(results) >= limit:
            break
    log.info("SoundCloud API вернул %s треков по запросу %r", len(results), query)
    return results


_PERMALINK_RE = re.compile(
    r'"(?:permalink_url|uri)":"(https:\\?/\\?/soundcloud\.com/[^"]+/[^"]+-\d+)"'
)
_HREF_RE = re.compile(r'href="(/[^"/?#]+/[^"/?#]+-\d+)"')


async def _search_scrape(query: str, limit: int) -> list[dict]:
    """Резервный поиск: ссылки со страницы + метаданные через oEmbed."""
    session = await http.session()
    try:
        async with session.get(
            f"https://soundcloud.com/search?q={quote(query)}", timeout=http.timeout(20)
        ) as resp:
            html = await resp.text(errors="ignore")
    except Exception as exc:  # noqa: BLE001
        log.warning("резервный поиск не удался: %s", exc)
        return []

    urls: list[str] = []
    for match in _PERMALINK_RE.finditer(html.replace("\\/", "/")):
        urls.append(match.group(1))
    if not urls:
        for match in _HREF_RE.finditer(html):
            urls.append("https://soundcloud.com" + match.group(1))

    unique: list[str] = []
    for url in urls:
        if url not in unique:
            unique.append(url)
        if len(unique) >= limit:
            break

    if not unique:
        return []

    tracks = await asyncio.gather(*(resolve_track(url) for url in unique))
    results = [t for t in tracks if t]
    log.info("резервный поиск вернул %s треков", len(results))
    return results[:limit]


# --------------------------------------------------------------------------- #
# метаданные трека
# --------------------------------------------------------------------------- #
async def resolve_track(url: str) -> dict | None:
    """Название / автор / обложка трека через oEmbed (без client_id)."""
    session = await http.session()
    try:
        async with session.get(OEMBED, params={"format": "json", "url": url}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("oEmbed не ответил: %s", exc)
        return None

    if not isinstance(data, dict):
        return None
    title = (data.get("title") or "").strip()
    author = (data.get("author_name") or "").strip()

    # oEmbed отдаёт вид "Песня by Автор" - убираем хвост, если он совпадает с автором.
    if author and title.lower().endswith(" by " + author.lower()):
        title = title[: -len(" by " + author)].strip()
    elif not author and " - " in title:
        author, title = title.split(" - ", 1)
        author, title = author.strip(), title.strip()

    if not title and not author:
        return None
    if not title:
        title = urlparse(url).path.rsplit("/", 1)[-1]
    if not author:
        author = urlparse(url).path.split("/")[1] if urlparse(url).path.count("/") else ""

    return {
        "url": data.get("url") or url,
        "title": title,
        "author": author,
        "duration": 0,
        "cover": (data.get("thumbnail_url") or "").replace("-large", "-t200x200"),
    }


# --------------------------------------------------------------------------- #
# скачивание (yt-dlp отдельным процессом)
# --------------------------------------------------------------------------- #
# Формат выбираем прогрессивный: у SoundCloud это mp3 с Content-Type audio/mpeg.
# HLS-плейлисты (.m3u8) не качаем - их Telegram не принимает даже как документ.
DIRECT_FORMAT = "bestaudio[protocol^=http]/bestaudio[ext=m4a]/bestaudio"
def build_command(url: str, output_template: str) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--no-color",
        "--socket-timeout",
        "20",
        "--retries",
        "3",
        "--fragment-retries",
        "3",
        "-f",
        DIRECT_FORMAT + "/best",
        "-o",
        output_template,
        "--print",
        "after_move:filepath",
    ]
    if CONVERT_TO_MP3 and FFMPEG_PATH:
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    cmd.append(url)
    return cmd


def child_env() -> dict[str, str]:
    """Заставляем yt-dlp печатать пути в UTF-8, иначе кириллица в пути портится."""
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def _pick_download(out: str, prefix: str) -> Path | None:
    """Найти скачанный файл: сначала по тому, что напечатал yt-dlp, иначе ищем в папке."""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    audio = (
        ".m4a", ".mp3", ".opus", ".ogg", ".mp4", ".webm", ".aac", ".wav", ".flac", ".m4b", ".opus"
    )
    for line in reversed(lines):
        if line.lower().endswith(audio) and Path(line).is_file():
            return Path(line)

    leftovers = [item for item in TMP_DIR.glob(f"{prefix}*") if item.is_file()]
    if leftovers:
        newest = max(leftovers, key=lambda item: item.stat().st_mtime)
        log.warning("путь из вывода не подошёл, беру самый свежий файл: %s", newest)
        return newest
    return None


async def download(url: str) -> Path | None:
    prefix = f"sc_{uuid4().hex[:12]}"
    output_template = str(TMP_DIR / f"{prefix}.%(ext)s")
    cmd = build_command(url, output_template)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
    except FileNotFoundError:
        log.error("не найден интерпретатор Python для запуска yt-dlp")
        return None

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        log.warning("yt-dlp не ответил за %s сек", DOWNLOAD_TIMEOUT)
        _cleanup_dir(TMP_DIR, ("sc_",))
        return None

    out = (stdout or b"").decode("utf-8", errors="ignore")
    err = (stderr or b"").decode("utf-8", errors="ignore").strip()

    if proc.returncode != 0:
        if "No module named yt_dlp" in err:
            log.error("не установлен yt-dlp: pip install -U yt-dlp")
        else:
            log.warning("yt-dlp упал (%s): %s", proc.returncode, err[-400:])
        _cleanup_dir(TMP_DIR, ("sc_",))
        return None

    path = _pick_download(out, prefix)
    if path is None:
        log.warning("yt-dlp не указал путь к файлу: %s", (out[-300:] or err[-300:]))
        _cleanup_dir(TMP_DIR, ("sc_",))
        return None

    size = path.stat().st_size
    if size <= 0:
        path.unlink(missing_ok=True)
        return None
    if size > MAX_UPLOAD_BYTES:
        log.warning("файл %s больше лимита Telegram", human_size(size))
        path.unlink(missing_ok=True)
        return None

    log.info("скачан трек %s (%s)", path.name, human_size(size))
    return path


def _cleanup_dir(directory: Path, prefixes: tuple[str, ...]) -> None:
    if not directory.is_dir():
        return
    for item in directory.glob("*"):
        if item.is_file() and item.name.startswith(prefixes):
            item.unlink(missing_ok=True)