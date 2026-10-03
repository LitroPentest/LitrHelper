"""LitrHelperBot — инлайн-бот Telegram (TikTok без водяного знака + SoundCloud).

Запуск:
    pip install -r requirements.txt
    вписать BOT_TOKEN в .env
    python bot.py

Как это работает:
    @LitrHelperBot <ссылка tiktok>    -> кнопка «Скачать видео»
    @LitrHelperBot <ссылка soundcloud> -> кнопка «Скачать музыку»
    @LitrHelperBot <Артист - Песня>    -> список треков SoundCloud

Бот показывает inline-сообщение с кнопкой, а по нажатию заменяет это же
сообщение в чате готовым файлом.

Telegram-слой написан на голом aiohttp (src/tg.py), без aiogram: он весит
~170 МБ RAM только на импорте, здесь же весь бот занимает ~35 МБ.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import logging.handlers
import secrets
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import (  # noqa: E402
    BOT_TIMEOUT,
    BOT_TOKEN,
    CONVERT_TO_MP3,
    DATA_DIR,
    DELETE_TMP_FILES_AFTER_SEND,
    FFMPEG_PATH,
    INLINE_CACHE_TIME,
    LOG_FILE,
    LOG_LEVEL,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_KNOWN_CHATS,
    MAX_PARALLEL_UPDATES,
    MAX_UPLOAD_BYTES,
    SEARCH_LIMIT,
    TELEGRAM_CA_BUNDLE,
    TELEGRAM_PROXY,
    TMP_DIR,
    VERIFY_TELEGRAM_SSL,
)
from src.cache import TTLCache, cleanup_loop  # noqa: E402
from src.downloaders import soundcloud, tiktok  # noqa: E402
from src.helpers import (  # noqa: E402
    classify_url,
    extract_url,
    format_duration,
    guess_media_type,
    human_size,
    split_track_query,
    truncate,
)
from src.http_client import http  # noqa: E402
from src.tg import (  # noqa: E402
    Telegram,
    TelegramConflict,
    TelegramError,
    TelegramTooManyRequests,
)

# --------------------------------------------------------------------------- #
# логирование
# --------------------------------------------------------------------------- #
log = logging.getLogger("bot")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)-9s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
        ),
    ],
)

# --------------------------------------------------------------------------- #
# состояние
# --------------------------------------------------------------------------- #
cache = TTLCache()
download_slot = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
stats = {"started": time.time(), "downloads": 0, "errors": 0}

# Telegram не даёт загружать новые файлы в инлайн-сообщения. Чтобы обойти это,
# бот кладёт файл в личный чат пользователя (где бот уже активен), достаёт
# file_id и уже им заменяет инлайн-сообщение. Поэтому тут храним id личных чатов.
private_chats: dict[int, int] = {}
bot_username: str = ""

WAIT_TEXT = "⏳ Нажми кнопку ниже — я заменю это сообщение готовым файлом."


def ram_mb() -> float | None:
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return None


def safe_rm(path: Path | None) -> None:
    if path and DELETE_TMP_FILES_AFTER_SEND:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def new_key() -> str:
    return "dl:" + secrets.token_hex(6)


def article(
    key: str, title: str, description: str, button: str, text: str
) -> dict:
    """Inline-результат с одной кнопкой. key=None — кнопки нет."""
    result: dict = {
        "type": "article",
        "id": secrets.token_hex(4),
        "title": truncate(title, 256),
        "description": truncate(description, 512),
        "input_message_content": {
            "message_text": text,
            "disable_web_page_preview": True,
        },
    }
    if key:
        result["reply_markup"] = {
            "inline_keyboard": [[{"text": button, "callback_data": key}]]
        }
    return result


# --------------------------------------------------------------------------- #
# инлайн-режим
# --------------------------------------------------------------------------- #
async def handle_inline_query(query: dict) -> None:
    raw = str(query.get("query") or "").strip()
    query_id = query["id"]

    if not raw:
        await tg.answer_inline_query(
            query_id,
            [
                article(
                    None,
                    "🎬 LitrHelperBot — что умею",
                    "Напиши рядом со мном: ссылку на TikTok, ссылку на SoundCloud "
                    "или «Артист - Название песни»",
                    "",
                    WAIT_TEXT,
                )
            ],
            INLINE_CACHE_TIME,
        )
        return

    url = extract_url(raw)
    kind = classify_url(url) if url else None

    try:
        if kind == "tiktok":
            results = tiktok_results(url)
        elif kind == "soundcloud":
            results = await soundcloud_link_results(url)
        elif url:
            results = [
                article(
                    None,
                    "🤔 Такую ссылку я не качаю",
                    "Умею только TikTok и SoundCloud. Напиши «Артист - Название песни», "
                    "если хочешь найти трек",
                    "",
                    WAIT_TEXT,
                )
            ]
        else:
            results = await soundcloud_search_results(raw)
    except Exception as exc:  # noqa: BLE001
        log.exception("inline-обработка упала: %s", exc)
        stats["errors"] += 1
        results = [article(None, "⚠️ Ошибка", truncate(str(exc), 200), "", WAIT_TEXT)]

    await tg.answer_inline_query(query_id, results, INLINE_CACHE_TIME)


def tiktok_results(url: str) -> list[dict]:
    key = new_key()
    cache.set(key, {"kind": "tiktok", "url": url, "title": "", "author": ""})
    return [
        article(
            key,
            "🎬 TikTok — видео без водяного знака",
            "Нажми кнопку: бот заменит это сообщение видео без водяного знака",
            "⬇️ Скачать видео",
            WAIT_TEXT,
        )
    ]


async def soundcloud_link_results(url: str) -> list[dict]:
    meta = await soundcloud.resolve_track(url)
    if not meta:
        return [article(None, "⚠️ Не удалось прочитать трек", "Проверь ссылку на SoundCloud", "", WAIT_TEXT)]

    key = new_key()
    cache.set(
        key,
        {
            "kind": "soundcloud",
            "url": meta.get("url") or url,
            "title": meta.get("title", ""),
            "author": meta.get("author", ""),
        },
    )
    who = meta.get("author") or "SoundCloud"
    return [
        article(
            key,
            f"🎵 {meta.get('title') or 'Трек'} — {who}",
            "Нажми кнопку: бот заменит это сообщение аудиофайлом",
            "⬇️ Скачать музыку",
            WAIT_TEXT,
        )
    ]


async def soundcloud_search_results(raw: str) -> list[dict]:
    author, title = split_track_query(raw)
    search_query = f"{author} {title}".strip() if title else raw
    tracks = await soundcloud.search(search_query, SEARCH_LIMIT)
    if not tracks:
        return [
            article(None, "🔍 Ничего не найдено", f"Запрос: {truncate(search_query, 60)}", "", WAIT_TEXT)
        ]

    results: list[dict] = []
    for track in tracks[:SEARCH_LIMIT]:
        key = new_key()
        cache.set(
            key,
            {
                "kind": "soundcloud",
                "url": track["url"],
                "title": track.get("title", ""),
                "author": track.get("author", ""),
            },
        )
        who = track.get("author") or "—"
        name = track.get("title") or "Трек"
        results.append(
            article(
                key,
                f"{who} — {name}",
                f"⏱ {format_duration(track.get('duration'))} · нажми, чтобы скачать музыку",
                "⬇️ Скачать",
                f"⏳ Скачиваю: {name}…",
            )
        )
    return results


# --------------------------------------------------------------------------- #
# нажатие кнопки
# --------------------------------------------------------------------------- #
async def handle_callback(callback: dict) -> None:
    payload = cache.pop(str(callback.get("data") or ""))
    inline_id = callback.get("inline_message_id")
    user_id = (callback.get("from") or {}).get("id")
    callback_id = callback["id"]

    if not payload:
        await tg.answer_callback_query(callback_id, "Кнопка устарела — напиши запрос заново", True)
        return

    await tg.answer_callback_query(callback_id, "⏳ Качаю, секунд 10–30…")
    log.info("скачивание: %s", payload.get("url"))

    try:
        await deliver(inline_id, payload, user_id)
        stats["downloads"] += 1
    except Exception as exc:  # noqa: BLE001
        stats["errors"] += 1
        log.exception("ошибка при отправке файла: %s", exc)
        await replace_text(inline_id, "❌ Не получилось скачать файл. Ссылка могла быть удалена.")
    finally:
        gc.collect()


# Telegram запрещает загружать новые файлы в инлайн-сообщения (см. доки
# editMessageMedia), поэтому идём по цепочке: прямая ссылка -> file_id -> текст.
DEFAULT_TYPE = {"tiktok": "video", "soundcloud": "audio"}

# Загрузить файл можно только в личку, поэтому для аудио личный чат обязателен.
NEED_PRIVATE_CHAT = (
    "⚠️ Чтобы приложить аудиофайл к этому сообщению, Telegram требует личный чат "
    "с ботом.\n\n"
    "Нажми кнопку ниже и отправь /start — это нужно один раз, дальше всё "
    "приложится сюда сразу."
)


async def collect_links(kind: str, payload: dict) -> tuple[list[str], str, str]:
    """(ссылки на файл, название, автор). Ничего не качает.

    SoundCloud ссылок не возвращает намеренно: его mp3 лежат по адресам вида
    s9KBLvMpTz3d.128.mp3, и Telegram показывает имя из тегов файла, а переданные
    title/performer для медиа по ссылке игнорирует. Поэтому аудио всегда
    качается и уходит через file_id — там название задаёт бот.
    """
    title = str(payload.get("title") or "")
    author = str(payload.get("author") or "")

    if kind == "tiktok":
        links, meta = await tiktok.direct_links(payload["url"])
        meta_title, meta_author = tiktok.describe(meta)
        return links, title or meta_title, author or meta_author

    return [], title, author


async def collect_file(kind: str, payload: dict) -> Path | None:
    if kind == "tiktok":
        return await tiktok.download(payload["url"])
    return await soundcloud.download(payload["url"])


def audio_meta(media_type: str, title: str, author: str) -> dict[str, str] | None:
    """InputMediaAudio умеет показывать исполнителя и название."""
    if media_type != "audio" or not (title or author):
        return None
    extra = {}
    if title:
        extra["title"] = title
    if author:
        extra["performer"] = author
    return extra or None


async def deliver(inline_id: str | None, payload: dict, user_id: int | None) -> None:
    if not inline_id:
        return

    kind = str(payload.get("kind") or "soundcloud")
    default_type = DEFAULT_TYPE.get(kind, "document")
    path: Path | None = None

    async with download_slot:
        try:
            # --- 1. прямая ссылка: Telegram скачает файл сам, бот даже не качает.
            # Только для видео: у аудио Telegram показывает имя из тегов файла,
            # а title/performer для медиа по ссылке игнорирует (см. collect_links).
            links, title, author = await collect_links(kind, payload)
            video_links = [
                link
                for link in links[:3]
                if (guess_media_type(link) or default_type) != "audio"
            ]

            chat_id = private_chats.get(user_id) if user_id else None

            # Аудио уходит только через загрузку файла, а она возможна лишь в личке.
            # Не качаем впустую — сразу объясняем, что надо нажать кнопку.
            if not video_links and chat_id is None:
                await replace_text(inline_id, NEED_PRIVATE_CHAT, open_bot_button())
                return

            for link in video_links:
                media_type = guess_media_type(link) or default_type
                try:
                    await tg.edit_media_url(
                        inline_id, media_type, link,
                        extra=audio_meta(media_type, title, author), timeout=BOT_TIMEOUT,
                    )
                    log.info("отправлено по прямой ссылке (%s): %s", media_type, link[:80])
                    return
                except TelegramError as exc:
                    log.warning("ссылка не подошла (%s): %s", exc.description, link[:80])

            # --- 2. качаем файл и получаем file_id через личку (100% рабочий путь)
            path = await collect_file(kind, payload)
            if path is None:
                await replace_text(inline_id, "❌ Не получилось скачать файл по ссылке.")
                return

            media_type = guess_media_type(path.name) or default_type
            extra = audio_meta(media_type, title, author)

            if chat_id is None:
                await replace_text(inline_id, NEED_PRIVATE_CHAT, open_bot_button())
                return

            file_id, used_type = await upload_for_file_id(chat_id, media_type, path, extra)
            if file_id:
                try:
                    await tg.edit_media_id(
                        inline_id, used_type, file_id,
                        extra=audio_meta(used_type, title, author), timeout=BOT_TIMEOUT,
                    )
                    log.info("отправлено по file_id (%s): %s", used_type, extra or "")
                    return
                except TelegramError as exc:
                    log.warning("file_id не подошёл: %s", exc)

            # --- 3. крайний случай: отдаём файл в личку и коротко говорим об этом
            try:
                await tg.upload_media(
                    chat_id, media_type, str(path), extra=extra, timeout=BOT_TIMEOUT
                )
            except TelegramError as exc:
                log.warning("и в личку отправить не вышло: %s", exc.description)
            await replace_text(inline_id, "⬇️ Файл отправлен тебе в личку.")
        except TelegramError as exc:
            log.warning("Telegram отклонил файл: %s", exc)
            await replace_text(
                inline_id,
                "⚠️ Файл не отправился.\n" + truncate(exc.description or str(exc), 300),
            )
        finally:
            safe_rm(path)


async def upload_for_file_id(
    chat_id: int, media_type: str, path: Path, extra: dict[str, str] | None = None
) -> tuple[str, str]:
    """Загрузить файл в личку, забрать file_id и сразу убрать сообщение.

    Возвращает (file_id, тип). Тип может отличаться от запрошенного: mp3
    Telegram принимает и как audio, и как document, а вот неизвестный формат
    только как document.
    """
    types = [media_type] + (["document"] if media_type != "document" else [])
    for kind in types:
        try:
            message = await tg.upload_media(
                chat_id, kind, str(path), extra=extra if kind != "document" else None,
                timeout=BOT_TIMEOUT,
            )
        except TelegramError as exc:
            log.warning("загрузка в личку не удалась (%s): %s", kind, exc.description)
            continue

        file_id = tg.file_id_of(message, kind)
        try:
            await tg.delete_message(chat_id, message["message_id"])
        except TelegramError as exc:
            log.warning("временное сообщение не удалилось: %s", exc.description)
        if file_id:
            return file_id, kind
    return "", media_type


def open_bot_button() -> dict | None:
    """Кнопка «открыть бота» — нужна один раз, чтобы получить file_id."""
    if not bot_username:
        return None
    return {
        "inline_keyboard": [[{
            "text": "🔓 Открыть бота и нажать /start",
            "url": f"https://t.me/{bot_username}?start=dl",
        }]]
    }


async def replace_text(message_id: str | None, text: str, markup: dict | None = None) -> None:
    if not message_id:
        return
    try:
        await tg.edit_text(message_id, text, reply_markup=markup)
    except TelegramError as exc:
        log.warning("не удалось заменить текст: %s", exc)


# --------------------------------------------------------------------------- #
# обычные сообщения
# --------------------------------------------------------------------------- #
HELP = (
    "🎬 <b>LitrHelperBot</b> — скачивание без заморочек.\n\n"
    "Как пользоваться: в <b>любом</b> чате напиши мой ник и пробел.\n\n"
    "1️⃣ <code>@LitrHelperBot ссылка_на_тикток</code>\n"
    "   → кнопка «Скачать видео» (без водяного знака)\n\n"
    "2️⃣ <code>@LitrHelperBot ссылка_на_саундклауд</code>\n"
    "   → кнопка «Скачать музыку» (файл трека)\n\n"
    "3️⃣ <code>@LitrHelperBot Артист - Название песни</code>\n"
    "   → список найденных треков SoundCloud, нажми нужный — файл придёт в чат\n\n"
    "Файлы приходят прямо в чат, где ты написал запрос."
)

START_BUTTON = {
    "inline_keyboard": [[{"text": "🎬 Открыть инлайн-режим", "switch_inline_query_current_chat": ""}]]
}


async def handle_message(message: dict) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return

    sender = message.get("from") or {}
    command = str(message.get("text") or "").strip().split("@")[0].split(" ")[0].lower()
    is_private = chat.get("type") == "private"
    user = sender.get("first_name") or "друг"

    # Личный чат запоминаем: он нужен, чтобы загрузить файл и достать file_id -
    # в инлайн-сообщение Telegram новые файлы загружать запрещает.
    if is_private and sender.get("id"):
        if sender["id"] not in private_chats:
            log.info("личный чат %s запомнил пользователь %s", chat_id, sender["id"])
        private_chats[sender["id"]] = chat_id
        while len(private_chats) > MAX_KNOWN_CHATS:
            private_chats.pop(next(iter(private_chats)))

    if command in {"/start", "/help"}:
        await tg.send_message(
            chat_id, f"Привет, {user}! 👋\n\n{HELP}", reply_markup=START_BUTTON
        )
    elif command == "/stats":
        await tg.send_message(chat_id, stats_text())
    elif is_private and command == "":
        await tg.send_message(chat_id, HELP, reply_markup=START_BUTTON)
    elif is_private:
        await tg.send_message(
            chat_id,
            "Чтобы скачать — напиши мой ник в любом чате:\n"
            "<code>@LitrHelperBot ссылка_на_тикток</code>\n"
            "<code>@LitrHelperBot ссылка_на_саундклауд</code>\n"
            "<code>@LitrHelperBot Артист - Песня</code>",
        )


def stats_text() -> str:
    ram = ram_mb()
    uptime = int(time.time() - stats["started"])
    hours, rest = divmod(uptime, 3600)
    minutes, seconds = divmod(rest, 60)
    mp3 = "вкл" if (CONVERT_TO_MP3 and FFMPEG_PATH) else "вкл (файл приходит как есть)"
    lines = [
        "📊 <b>Статистика</b>",
        f"⏱ Аптайм: {hours}ч {minutes}м {seconds}с",
        f"⬇️ Отправлено файлов: {stats['downloads']}",
        f"⚠️ Ошибок: {stats['errors']}",
        f"🗂 Кэш кнопок: {len(cache)} / {cache.max_items} (TTL {cache.ttl:.0f}с)",
        f"🎧 Конвертация: {mp3}",
        f"📦 Лимит файла: {human_size(MAX_UPLOAD_BYTES)}",
        f"👥 Личных чатов: {len(private_chats)}",
    ]
    if ram:
        lines.append(f"🧠 ОЗУ процесса: {ram:.0f} МБ")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# приём апдейтов и long polling
# --------------------------------------------------------------------------- #
running_tasks: set[asyncio.Task] = set()
update_slot = asyncio.Semaphore(MAX_PARALLEL_UPDATES)


async def handle_update(update: dict) -> None:
    if "inline_query" in update:
        await handle_inline_query(update["inline_query"])
    elif "callback_query" in update:
        await handle_callback(update["callback_query"])
    elif "message" in update:
        await handle_message(update["message"])


def dispatch(update: dict) -> None:
    """Каждый апдейт - отдельная задача, но не больше MAX_PARALLEL_UPDATES одновременно."""
    task = asyncio.create_task(_safe_handle(update))
    running_tasks.add(task)
    task.add_done_callback(running_tasks.discard)


async def _safe_handle(update: dict) -> None:
    try:
        async with update_slot:  # не даём задачам копиться при флуде
            await handle_update(update)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        stats["errors"] += 1
        log.exception("update не обработан: %s", exc)


async def polling_loop() -> None:
    offset = 0
    log.info("Жду апдейтов…")
    while True:
        try:
            updates = await tg.get_updates(offset, timeout=45)
        except TelegramConflict:
            log.warning("Бот уже запущен в другом процессе — подожду 10 секунд")
            await asyncio.sleep(10)
            continue
        except TelegramTooManyRequests as exc:
            wait = max(1, exc.retry_after)
            log.warning("Telegram попросил подождать %s сек", wait)
            await asyncio.sleep(wait)
            continue
        except TelegramError as exc:
            log.warning("getUpdates: %s — повтор через 5 сек", exc)
            await asyncio.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            dispatch(update)


# --------------------------------------------------------------------------- #
# запуск / остановка
# --------------------------------------------------------------------------- #
def purge_tmp_files() -> int:
    removed = 0
    if TMP_DIR.is_dir():
        for item in TMP_DIR.glob("*"):
            try:
                item.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed


def make_telegram(*, verify: bool = True) -> Telegram:
    return Telegram(
        BOT_TOKEN,
        timeout=BOT_TIMEOUT,
        proxy=TELEGRAM_PROXY,
        verify=verify,
        ca_bundle=TELEGRAM_CA_BUNDLE,
    )


async def ask_bot() -> dict:
    """Узнать кто я. Если сертификат api.telegram.org подменён — пробуем без проверки."""
    global tg

    tg = make_telegram()
    try:
        return await tg.get_me()
    except TelegramError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in (exc.description or "") or not VERIFY_TELEGRAM_SSL:
            raise

    log.warning("SSL api.telegram.org не проходит проверку — похоже, трафик перехватывают")
    log.warning("Повторяю подключение без проверки сертификата (задай TELEGRAM_CA_BUNDLE, чтобы вернуть проверку)")
    await tg.close()
    tg = make_telegram(verify=False)
    return await tg.get_me()


async def main() -> None:
    global bot_username

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    if not BOT_TOKEN:
        log.error("BOT_TOKEN не найден. Впиши токен от @BotFather в файл .env рядом с bot.py")
        sys.exit(1)

    gc.freeze()  # меньше работы сборщика мусора на долгих сессиях
    log.info("Временных файлов очищено: %s", purge_tmp_files())

    try:
        me = await ask_bot()
    except TelegramError as exc:
        log.error("Telegram не отвечает: %s", exc.description)
        log.error("Проверь BOT_TOKEN в файле .env — его выдаёт @BotFather")
        sys.exit(1)

    if not me.get("supports_inline_queries"):
        log.warning("Инлайн-режим ВЫКЛЮЧЕН! Включи его у @BotFather: /setinline")

    bot_username = str(me.get("username") or "")

    try:
        await tg.set_commands(
            [
                {"command": "start", "description": "Как пользоваться"},
                {"command": "help", "description": "Справка"},
                {"command": "stats", "description": "Статистика и память"},
            ]
        )
    except TelegramError as exc:
        log.warning("не удалось выставить команды: %s", exc)

    ram = ram_mb()
    log.info("Бот запущен как @%s (id %s)%s", me.get("username"), me.get("id"),
             f" | ОЗУ: {ram:.0f} МБ" if ram else "")

    poller = asyncio.create_task(polling_loop())
    cleanup = asyncio.create_task(cleanup_loop(cache))
    try:
        await poller
    except asyncio.CancelledError:
        pass
    finally:
        for task in (poller, cleanup):
            task.cancel()
        for task in list(running_tasks):
            task.cancel()
        await http.close()
        await tg.close()
        purge_tmp_files()
        gc.unfreeze()
        gc.collect()


tg: Telegram  # объявляется в main()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
    # SystemExit не ловим: код возврата уже выведен в main()