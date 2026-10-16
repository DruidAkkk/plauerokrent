"""Вежливая просьба об отзыве после успешной сделки (#23).

Чистая политика без сети. Просим один раз на сделку, только после успешного завершения,
без спора/возврата и без «платы» за положительную оценку (честная просьба — безопасный путь,
в отличие от накрутки). Текст нейтральный: просим оценить, а не поставить 5 звёзд.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TEMPLATE = "Спасибо за покупку! Если всё прошло хорошо, будем благодарны за честный отзыв о сделке."

# Ключи настроек (хранилище key-value): включение и текст просьбы об отзыве.
ENABLED_KEY = "review_request_enabled"
TEXT_KEY = "review_request_text"


def should_ask(storage) -> bool:
    """Просить ли отзыв. По умолчанию ВЫКЛ (opt-in): это исходящее сообщение реальному
    покупателю, включать его должен владелец осознанно (Автоматизация → Просьба об отзыве)."""
    return storage.get_setting(ENABLED_KEY) == "1"


def review_text(storage) -> str:
    """Текст просьбы: пользовательский из настроек либо нейтральный шаблон по умолчанию."""
    return (storage.get_setting(TEXT_KEY) or "").strip() or TEMPLATE


async def ask_after_completion(storage, market, chat_id: str | None, *, order_id: str | None = None) -> bool:
    """Отправить просьбу об отзыве в чат после успешного завершения сделки.

    Единая точка для всех видов аренды (Steam/KoSell/PUBG). Ничего не делает, если
    функция выключена или нет чата. Сбой отправки не роняет завершение аренды.
    Возвращает True, если сообщение отправлено."""
    if not chat_id or not should_ask(storage):
        return False
    try:
        await market.send_message(chat_id, review_text(storage))
        storage.log("review_asked", data={"order": order_id, "chat": chat_id})
        return True
    except Exception:  # noqa: BLE001 — просьба об отзыве не критична, не ломаем завершение
        return False


@dataclass
class ReviewAsker:
    template: str = TEMPLATE
    _asked: set = field(default_factory=set)

    def message(self, order_id: str, *, completed: bool, disputed: bool = False,
                refunded: bool = False, already_reviewed: bool = False) -> str | None:
        """Текст просьбы либо None, если просить нельзя/не нужно. Помечает сделку как «спрошенную»."""
        if not completed or disputed or refunded or already_reviewed:
            return None
        if order_id in self._asked:
            return None
        self._asked.add(order_id)
        return self.template
