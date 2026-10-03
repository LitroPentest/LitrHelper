"""Крошечный кэш с временем жизни.

Нужен, чтобы передать ссылку с inline-результата в callback-кнопку
(callback_data ограничен 64 байтами) и не держать её в FSM-хранилище.

Данные живут в обычном dict и целиком удаляются по TTL, поэтому не растут.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from config import CACHE_MAX_ITEMS, CACHE_TTL


class TTLCache:
    def __init__(self, ttl: float = CACHE_TTL, max_items: int = CACHE_MAX_ITEMS) -> None:
        self.ttl = float(ttl)
        self.max_items = int(max_items)
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    # --- внутреннее --------------------------------------------------------
    def _drop_expired(self, now: float) -> None:
        expired = [key for key, (exp, _) in self._items.items() if exp <= now]
        for key in expired:
            self._items.pop(key, None)

    def _make_room(self, now: float) -> None:
        self._drop_expired(now)
        while len(self._items) >= self.max_items:
            oldest = min(self._items, key=lambda key: self._items[key][0])
            self._items.pop(oldest, None)

    # --- публичное ---------------------------------------------------------
    def set(self, key: str, value: Any) -> None:
        """Синхронная запись (слот asyncio не переключается)."""
        now = time.monotonic()
        if len(self._items) >= self.max_items:
            self._make_room(now)
        self._items[key] = (now + self.ttl, value)

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        exp, value = item
        if exp <= time.monotonic():
            self._items.pop(key, None)
            return None
        return value

    def pop(self, key: str) -> Any | None:
        """Забрать значение и удалить его (кнопку можно нажать один раз)."""
        item = self._items.pop(key, None)
        if item is None or item[0] <= time.monotonic():
            return None
        return item[1]

    def purge(self) -> int:
        """Убрать всё протухшее. Возвращает количество удалённых записей."""
        before = len(self._items)
        self._drop_expired(time.monotonic())
        return before - len(self._items)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    def stats(self) -> dict[str, int]:
        return {"size": len(self._items), "limit": self.max_items, "ttl": int(self.ttl)}


async def cleanup_loop(cache: TTLCache, interval: int = 300) -> None:
    """Фоновая задача: раз в interval секунд чистит протухшие записи."""
    while True:
        try:
            await asyncio.sleep(interval)
            cache.purge()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - фоновая задача не должна падать
            pass