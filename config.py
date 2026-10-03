"""Конфигурация LitrHelperBot.

Все настройки читаются из переменных окружения / файла .env рядом с bot.py,
поэтому токен не надо зашивать в код.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TMP_DIR = DATA_DIR / "downloads"
LOG_FILE = DATA_DIR / "bot.log"


def _load_dotenv(path: Path) -> None:
    """Минимальный парсер .env (чтобы не тянуть лишнюю зависимость)."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


_load_dotenv(BASE_DIR / ".env")

# --- токен ------------------------------------------------------------------
BOT_TOKEN: str = (os.environ.get("BOT_TOKEN") or "").strip()

# --- лимиты и таймауты ------------------------------------------------------
MAX_CONCURRENT_DOWNLOADS = _int_env("MAX_CONCURRENT_DOWNLOADS", 2, 1, 8)  # одновременных загрузок
MAX_PARALLEL_UPDATES = _int_env("MAX_PARALLEL_UPDATES", 16, 1, 64)  # одновременных апдейтов
CACHE_TTL = _int_env("CACHE_TTL", 900, 60, 3600)  # сколько живёт нажатая кнопка, сек
CACHE_MAX_ITEMS = _int_env("CACHE_MAX_ITEMS", 600, 50, 5000)  # максимум записей в кэше
SEARCH_LIMIT = _int_env("SEARCH_LIMIT", 8, 1, 20)  # треков в выдаче SoundCloud
INLINE_CACHE_TIME = _int_env("INLINE_CACHE_TIME", 5, 0, 60)  # кэш апсерча инлайна
DOWNLOAD_TIMEOUT = _int_env("DOWNLOAD_TIMEOUT", 300, 30, 1200)  # таймаут загрузки, сек
API_TIMEOUT = _int_env("API_TIMEOUT", 25, 5, 120)  # таймаут запросов к API
BOT_TIMEOUT = _int_env("BOT_TIMEOUT", 300, 30, 1200)  # таймаут отправки в Telegram

# Сколько личных чатов помним. Telegram запрещает загружать новые файлы в
# инлайн-сообщения, поэтому для file_id бот кладёт файл в личку пользователя.
# Идентификаторы - это просто int, памяти на них почти нет.
MAX_KNOWN_CHATS = _int_env("MAX_KNOWN_CHATS", 2000, 10, 100000)

# Телеграм боту нельзя заливать файлы больше 50 МБ.
MAX_UPLOAD_BYTES = _int_env("MAX_UPLOAD_MB", 49, 1, 49) * 1024 * 1024

# --- ffmpeg / конвертация ---------------------------------------------------
CONVERT_TO_MP3 = _bool_env("CONVERT_TO_MP3", True)
FFMPEG_PATH: str | None = shutil.which("ffmpeg")

# --- сеть -------------------------------------------------------------------
HTTP_CONNECTIONS = _int_env("HTTP_CONNECTIONS", 8, 2, 32)

# --- Telegram: прокси и SSL -------------------------------------------------
# Если api.telegram.org перехватывают (частая история: DPI/антивирус/VPN),
# есть три варианта - по порядку приоритета:
#   1) TELEGRAM_CA_BUNDLE=ca.pem    указать файл CA, которым подписан перехват
#   2) VERIFY_TELEGRAM_SSL=0          вообще не проверять сертификат (проще, но небезопасно)
#   3) TELEGRAM_PROXY=http://host:port идти через прокси (для заблокированных сетей)
TELEGRAM_PROXY: str = (os.environ.get("TELEGRAM_PROXY") or "").strip()
TELEGRAM_CA_BUNDLE: str = (os.environ.get("TELEGRAM_CA_BUNDLE") or "").strip()
VERIFY_TELEGRAM_SSL = _bool_env("VERIFY_TELEGRAM_SSL", True)

# --- логи -------------------------------------------------------------------
LOG_LEVEL = (os.environ.get("LOG_LEVEL") or "INFO").upper()

# --- прочее -----------------------------------------------------------------
DELETE_TMP_FILES_AFTER_SEND = _bool_env("DELETE_TMP_FILES_AFTER_SEND", True)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)