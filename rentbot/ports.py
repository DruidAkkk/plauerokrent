"""Порты (интерфейсы). Конкретные реализации — площадки, Steam, уведомления —
подключаются снаружи. Благодаря этому движок аренды тестируется без сети.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class OrderEvent:
    """Оплаченный заказ, пришёл с площадки (WebSocket или опрос)."""
    marketplace: str
    deal_id: str       # уникальный id сделки — ключ идемпотентности
    lot_id: str
    buyer_id: str
    buyer_name: str
    chat_id: str
    price: float


@dataclass
class ChatMessage:
    marketplace: str
    chat_id: str
    buyer_id: str
    text: str


class Marketplace(Protocol):
    """Адаптер торговой площадки (Playerok, в будущем FunPay)."""
    name: str

    async def send_message(self, chat_id: str, text: str) -> None: ...
    async def set_lot_active(self, lot_id: str, active: bool) -> None: ...
    # базовые функции площадки (не про аренду):
    async def send_file(self, chat_id: str, file_path: str, caption: str = "") -> None: ...
    async def bump_item(self, lot_id: str, price: float) -> None: ...
    # поток событий отдаётся движку через callbacks, см. engine.run()


class SteamBackend(Protocol):
    """Операции над Steam-аккаунтом. guard — локально; остальное — сеть/сайдкар."""
    def guard_code(self, shared_secret: str) -> str: ...
    async def rotate_credentials(self, *, login: str, password: str, shared_secret: str,
                                 identity_secret: str, proxy: str | None) -> str:
        """Сменить пароль и деавторизовать все сессии. Вернуть НОВЫЙ пароль.
        Бросить исключение, если не удалось (аккаунт уйдёт в карантин)."""
        ...

    async def deauth(self, *, login: str, password: str, shared_secret: str,
                     proxy: str | None) -> dict:
        """«Выйти со всех устройств» без смены пароля (авто-кик по окончании аренды).
        Возвращает dict сайдкара {ok, message, total?, sessions?}. Не бросает — об ошибке
        сообщает через ok=False/message."""
        ...


class Notifier(Protocol):
    """Уведомления владельцу (Telegram)."""
    async def notify(self, text: str) -> None: ...
