"""Минимальный клиент Telegram Bot API на голом aiohttp.

Почему не aiogram: один только импорт aiogram поднимает процесс до ~170 МБ
(сотни pydantic-моделей). Здесь используется только то, что реально нужно боту:
long polling, инлайн-ответы, кнопки и отправка файла. Итого ~32 МБ на простое.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from pathlib import Path
from typing import Any, Iterable

import aiohttp
from aiohttp import FormData

log = logging.getLogger("bot.tg")

API = "https://api.telegram.org"


class TelegramError(Exception):
    """Бот API вернул ok=false или сеть не ответила."""

    def __init__(self, method: str, code: int, description: str, retry_after: int = 0) -> None:
        super().__init__(f"{method}: [{code}] {description}")
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after


class TelegramConflict(TelegramError):
    """409: бот уже запущен другим процессом."""


class TelegramTooManyRequests(TelegramError):
    """429: Telegram просит подождать."""


class Telegram:
    def __init__(
        self,
        token: str,
        timeout: int = 60,
        proxy: str = "",
        verify: bool = True,
        ca_bundle: str = "",
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.proxy = proxy or None
        self.verify = verify
        self.ca_bundle = ca_bundle or None
        self.base = f"{API}/bot{token}"
        self._session: aiohttp.ClientSession | None = None

    # --- соединение --------------------------------------------------------
    def _ssl_context(self) -> ssl.SSLContext | bool:
        if not self.verify:
            log.warning("Проверка SSL-сертификата для api.telegram.org ОТКЛЮЧЕНА")
            return False
        if self.ca_bundle:
            return ssl.create_default_context(cafile=self.ca_bundle)
        return True

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout, connect=15, sock_connect=15),
                connector=aiohttp.TCPConnector(ssl=self._ssl_context()),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # --- вызов API ---------------------------------------------------------
    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        files: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> Any:
        data = {key: value for key, value in (payload or {}).items() if value is not None}
        url = f"{self.base}/{method}"
        session = await self.session()
        client_timeout = aiohttp.ClientTimeout(
            total=timeout or self.timeout, connect=15, sock_connect=15, sock_read=timeout or self.timeout
        )

        handles: list[Any] = []
        try:
            if files:
                form = FormData()
                for key, value in data.items():
                    if isinstance(value, (dict, list)):
                        form.add_field(key, json.dumps(value, ensure_ascii=False))
                    else:
                        form.add_field(key, str(value))
                for field, path in files.items():
                    handle = open(path, "rb")  # noqa: SIM115
                    handles.append(handle)
                    form.add_field(field, handle, filename=Path(path).name)
                async with session.post(
                    url, data=form, timeout=client_timeout, proxy=self.proxy
                ) as resp:
                    result = await self._read(resp, method)
            else:
                async with session.post(
                    url, json=data, timeout=client_timeout, proxy=self.proxy
                ) as resp:
                    result = await self._read(resp, method)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TelegramError(method, 0, f"сеть: {exc}") from exc
        finally:
            for handle in handles:
                handle.close()
        return result

    @staticmethod
    async def _read(resp: aiohttp.ClientResponse, method: str) -> Any:
        try:
            body = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 - ответ может быть не JSON
            raise TelegramError(method, resp.status, f"не-JSON ответ: {exc}") from exc

        if body.get("ok"):
            return body.get("result")

        code = int(body.get("error_code") or resp.status)
        description = str(body.get("description") or "без описания")
        parameters = body.get("parameters") or {}
        error = TelegramError(method, code, description, int(parameters.get("retry_after") or 0))
        if code == 409:
            raise TelegramConflict(method, code, description) from None
        if code == 429:
            raise TelegramTooManyRequests(method, code, description, error.retry_after) from None
        raise error from None

    # --- методы, которые реально нужны ------------------------------------
    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_updates(self, offset: int, timeout: int = 45) -> Iterable[dict]:
        result = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query", "inline_query"],
            },
            timeout=timeout + 20,
        )
        return result or []

    async def answer_inline_query(
        self, inline_query_id: str, results: list[dict], cache_time: int = 5
    ) -> None:
        await self.call(
            "answerInlineQuery",
            {"inline_query_id": inline_query_id, "results": results, "cache_time": cache_time,
             "is_personal": True},
        )

    async def answer_callback_query(
        self, callback_query_id: str, text: str = "", show_alert: bool = False
    ) -> None:
        await self.call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert},
        )

    # --- медиа --------------------------------------------------------------
    # ВАЖНО (доки Telegram, editMessageMedia):
    #   "When an inline message is edited, a new file can't be uploaded;
    #    use a previously uploaded file via its file_id or specify a URL."
    # Поэтому для инлайн-сообщений есть ровно два рабочих способа:
    #   edit_media_url() - отдать Telegram прямую ссылку;
    #   edit_media_id()  - отдать file_id уже загруженного файла.
    # Загрузка файла (edit_media_file) в инлайн-сообщение не работает.
    _SEND_METHOD = {"video": "sendVideo", "audio": "sendAudio", "document": "sendDocument"}

    @staticmethod
    def _input_media(
        media_type: str, source: str, caption: str | None, extra: dict | None
    ) -> dict[str, Any]:
        media: dict[str, Any] = {"type": media_type, "media": source}
        if caption:
            media["caption"] = caption
        if media_type == "video":
            media["supports_streaming"] = True
        media.update(extra or {})
        return media

    async def edit_media_url(
        self,
        inline_message_id: str,
        media_type: str,
        url: str,
        caption: str | None = None,
        extra: dict | None = None,
        timeout: int | None = None,
    ) -> None:
        """Заменить инлайн-сообщение файлом по прямой ссылке (ничего не качаем)."""
        await self.call(
            "editMessageMedia",
            {
                "inline_message_id": inline_message_id,
                "media": self._input_media(media_type, url, caption, extra),
            },
            timeout=timeout,
        )

    async def edit_media_id(
        self,
        inline_message_id: str,
        media_type: str,
        file_id: str,
        caption: str | None = None,
        extra: dict | None = None,
        timeout: int | None = None,
    ) -> None:
        """Заменить инлайн-сообщение файлом, который Telegram уже хранит."""
        await self.call(
            "editMessageMedia",
            {
                "inline_message_id": inline_message_id,
                "media": self._input_media(media_type, file_id, caption, extra),
            },
            timeout=timeout,
        )

    async def edit_media_file(
        self,
        inline_message_id: str,
        media_type: str,
        path: str,
        caption: str | None = None,
        extra: dict | None = None,
        timeout: int | None = None,
    ) -> None:
        """Замена с загрузкой файла. Работает только для обычных сообщений."""
        await self.call(
            "editMessageMedia",
            {
                "inline_message_id": inline_message_id,
                "media": self._input_media(media_type, f"attach://{media_type}", caption, extra),
            },
            files={media_type: path},
            timeout=timeout,
        )

    async def upload_media(
        self,
        chat_id: int,
        media_type: str,
        path: str,
        caption: str | None = None,
        extra: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        """Загрузить файл в чат и вернуть Message (из него берём file_id).

        extra — поля Audio/Video (title, performer, supports_streaming). Без них
        Telegram берёт название из тегов файла, а у треков SoundCloud там мусор.
        """
        method = self._SEND_METHOD.get(media_type, "sendDocument")
        payload: dict[str, Any] = {"chat_id": chat_id, media_type: f"attach://{media_type}"}
        if caption:
            payload["caption"] = caption
        if media_type == "video":
            payload["supports_streaming"] = True
        payload.update(extra or {})
        return await self.call(method, payload, files={media_type: path}, timeout=timeout)

    @staticmethod
    def file_id_of(message: dict, media_type: str) -> str:
        media = message.get(media_type) or {}
        return str(media.get("file_id") or "")

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def edit_text(
        self, inline_message_id: str, text: str, reply_markup: dict | None = None
    ) -> None:
        await self.call(
            "editMessageText",
            {
                "inline_message_id": inline_message_id,
                "text": text,
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            },
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = "HTML",
        reply_markup: dict | None = None,
    ) -> None:
        await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "disable_web_page_preview": True,
            },
        )

    async def set_commands(self, commands: list[dict]) -> None:
        await self.call("setMyCommands", {"commands": commands})