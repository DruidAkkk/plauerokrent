"""Уведомления владельцу в Telegram (о заказах и сообщениях).

Реализует порт Notifier. Транспорт инъектируется — для тестов подставляется фейк,
в проде по умолчанию ходит на api.telegram.org через curl_cffi (как и Playerok).
Токен бота и chat_id хранятся в settings (токен — зашифрованным).
"""
from __future__ import annotations

from typing import Awaitable, Callable

TELEGRAM_API = "https://api.telegram.org"


class NullNotifier:
    """Заглушка: уведомления отключены."""
    async def notify(self, text: str) -> None:
        return None


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str,
                 transport: Callable[[str, dict], Awaitable[dict]] | None = None):
        self.token = token
        self.chat_id = chat_id
        self._transport = transport or self._http

    async def _http(self, method: str, payload: dict) -> dict:
        from curl_cffi import requests  # ленивый импорт: ядро/тесты не тянут сеть
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        async with requests.AsyncSession() as s:
            r = await s.post(url, json=payload, timeout=20)
            return r.json()

    async def notify(self, text: str) -> None:
        await self._transport("sendMessage", {
            "chat_id": self.chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        })
