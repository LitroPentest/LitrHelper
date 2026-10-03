"""Один общий aiohttp-клиент на весь бот.

Держать одну сессию дешевле, чем создавать новую на каждый запрос:
меньше памяти и меньше TCP-рукопожатий.
"""

from __future__ import annotations

import aiohttp

from config import API_TIMEOUT, HTTP_CONNECTIONS, USER_AGENT


class HttpClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=API_TIMEOUT, connect=15, sock_connect=15, sock_read=60
            )
            connector = aiohttp.TCPConnector(
                limit=HTTP_CONNECTIONS,
                ttl_dns_cache=600,
                enable_cleanup_closed=False,
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
        return self._session

    def timeout(self, total: int) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=total, connect=15, sock_connect=15, sock_read=60)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


http = HttpClient()