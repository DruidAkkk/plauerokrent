"""Вежливая просьба об отзыве после успешной сделки (#23).

Чистая политика без сети. Просим один раз на сделку, только после успешного завершения,
без спора/возврата и без «платы» за положительную оценку (честная просьба — безопасный путь,
в отличие от накрутки). Текст нейтральный: просим оценить, а не поставить 5 звёзд.
"""
from __future__ import annotations

from dataclasses import dataclass, field

TEMPLATE = "Спасибо за покупку! Если всё прошло хорошо, будем благодарны за честный отзыв о сделке."


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
