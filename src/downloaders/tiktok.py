"""TikTok: загрузка видео БЕЗ водяного знака.

Используются публичные сервисы-резолверы (tikwm.com, при отказе — tikdown.org).
Веса отдаются по прямой ссылке на CDN, поэтому бот не загружает ничего лишнего.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import aiohttp

from config import DOWNLOAD_TIMEOUT, MAX_UPLOAD_BYTES, TMP_DIR
from src.helpers import human_size
from src.http_client import http

log = logging.getLogger("bot.tiktok")

TIKWM_ENDPOINTS = ("https://www.tikwm.com/api/", "https://tikwm.com/api/")
TIKDOWN_ENDPOINT = "https://tikdown.org/api/ajax"
TIKWM_CDN = "https://www.tikwm.com"
CHUNK = 65536


async def resolve(url: str) -> dict | None:
    """Получить метаданные + ссылку на видео без водяного знака."""
    session = await http.session()
    payload = {
        "url": url,
        "hd": 1,
        "count": 0,
        "cursor": 0,
        "web": 1,
        "region": "US",
        "proxy": "",
        "device_id": "",
    }

    for index, endpoint in enumerate(TIKWM_ENDPOINTS):
        if index:
            await asyncio.sleep(1.2)  # бесплатный лимит tikwm: 1 запрос в секунду
        try:
            async with session.post(endpoint, json=payload) as resp:
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 - сервис может упасть/смениться
            log.warning("tikwm %s: %s", endpoint, exc)
            continue
        if isinstance(data, dict) and data.get("code") == 0 and isinstance(data.get("data"), dict):
            return data["data"]
        log.warning("tikwm %s вернул %s", endpoint, str(data)[:200])

    return await _resolve_via_tikdown(session, url)


async def _resolve_via_tikdown(session: aiohttp.ClientSession, url: str) -> dict | None:
    try:
        async with session.post(TIKDOWN_ENDPOINT, data={"url": url, "hd": "3"}) as resp:
            data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("tikdown: %s", exc)
        return None
    if isinstance(data, dict) and data.get("success") and isinstance(data.get("data"), dict):
        info = data["data"]
        return {
            "title": info.get("title") or info.get("text") or "TikTok",
            "author": info.get("author") or "",
            "cover": info.get("cover") or info.get("thumbnail") or "",
            "download": info.get("url") or info.get("download") or info.get("video"),
            "duration": info.get("duration") or 0,
        }
    log.warning("tikdown вернул %s", str(data)[:200])
    return None


def _pick_link(meta: dict) -> str | None:
    """Найти ссылку на видео. tikwm часто отдаёт относительный путь - дополняем доменом."""
    links = candidate_links(meta)
    return links[0] if links else None


# Порядок важен: сначала варианты без водяного знака, потом запасные.
LINK_KEYS = ("download", "hdplay", "play", "wmplay", "url", "video")


def candidate_links(meta: dict) -> list[str]:
    """Все прямые ссылки на видео из ответа tikwm/tikdown, без повторов.

    Telegram сам скачает файл по этой ссылке, поэтому боту качать его не нужно.
    """
    links: list[str] = []
    for key in LINK_KEYS:
        value = meta.get(key)
        if not isinstance(value, str) or not value:
            continue
        link = value if value.startswith("http") else TIKWM_CDN + value
        if link not in links:
            links.append(link)
    return links


def describe(meta: dict) -> tuple[str, str]:
    """(название, автор) из ответа tikwm. author там иногда словарь."""
    title = str(meta.get("title") or "").strip()
    author = meta.get("author")
    if isinstance(author, dict):
        author = author.get("unique_id") or author.get("nickname") or ""
    return title, str(author or "").strip()


async def direct_links(url: str) -> tuple[list[str], dict]:
    """(ссылки на видео без водяного знака, метаданные). Без скачивания."""
    meta = await resolve(url)
    if not meta:
        log.warning("не удалось разобрать ссылку: %s", url)
        return [], {}
    return candidate_links(meta), meta


async def download(url: str) -> Path | None:
    """Скачать видео во временный файл. Возвращает путь или None."""
    links, meta = await direct_links(url)
    if not links:
        return None
    link = links[0]

    suffix = Path(urlparse(link).path).suffix or ".mp4"
    if suffix.lower() not in {".mp4", ".webm", ".mov", ".mkv"}:
        suffix = ".mp4"
    target = TMP_DIR / f"tt_{uuid4().hex[:12]}{suffix}"

    session = await http.session()
    try:
        async with session.get(link, timeout=http.timeout(DOWNLOAD_TIMEOUT)) as resp:
            if resp.status >= 400:
                raise aiohttp.ClientError(f"HTTP {resp.status}")
            length = int(resp.headers.get("Content-Length") or 0)
            if length and length > MAX_UPLOAD_BYTES:
                raise ValueError(
                    f"видео слишком большое ({human_size(length)} > {human_size(MAX_UPLOAD_BYTES)})"
                )
            written = 0
            with target.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(CHUNK):
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        raise ValueError("видео слишком большое для Telegram")
                    fh.write(chunk)
        return target
    except Exception as exc:  # noqa: BLE001
        log.warning("скачивание видео не удалось: %s", exc)
        target.unlink(missing_ok=True)
        return None