"""Оценка риска покупателя по его истории сделок (#29).

Чистая логика без сети: решает, стоит ли предупредить владельца (Telegram/панель) о покупателе
с историей споров/возвратов. Ничего не блокирует и не скрывает — только подсказка человеку.
"""
from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("ok", "watch", "risky")


@dataclass(frozen=True)
class BuyerHistory:
    deals: int = 0       # завершённые сделки
    disputes: int = 0    # открытые покупателем споры
    refunds: int = 0     # возвраты
    blacklisted: bool = False  # ручная пометка владельца


def assess(h: BuyerHistory) -> tuple[str, str]:
    """Вернуть (уровень, причина). Ручной чёрный список всегда «risky»."""
    if h.blacklisted:
        return "risky", "в вашем чёрном списке"
    bad = h.disputes + h.refunds
    total = h.deals + bad
    if bad == 0:
        return "ok", "претензий не было"
    share = bad / total
    if bad >= 3 and share >= 0.5:
        return "risky", f"{bad} из {total} сделок со спором/возвратом"
    if bad >= 2 or share >= 0.34:
        return "watch", f"{bad} из {total} сделок со спором/возвратом"
    return "ok", f"единичная претензия ({bad} из {total})"
