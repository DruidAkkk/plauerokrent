"""Лесенка скидок постоянным арендаторам (#32).

Чистая политика без сети: по числу прошлых успешных аренд покупателя считает рекомендуемую
скидку и цену. Ничего не создаёт и не меняет лоты на площадке — только подсказка владельцу
(цену применяет человек), так что правила Playerok о запрете авто-создания лотов не затронуты.
"""
from __future__ import annotations

# (минимум успешных аренд, скидка в %) — по возрастанию порога.
DEFAULT_TIERS: tuple[tuple[int, int], ...] = ((3, 5), (6, 10), (10, 15))
MAX_DISCOUNT = 30


def discount_percent(completed_rentals: int, tiers=DEFAULT_TIERS) -> int:
    """Скидка (%) для покупателя с N успешными арендами; споры/возвраты в N не входят."""
    pct = 0
    for need, p in sorted(tiers):
        if completed_rentals >= need:
            pct = p
    return max(0, min(pct, MAX_DISCOUNT))


def loyal_price(base_price: float, completed_rentals: int, *, floor: float = 0.0,
                tiers=DEFAULT_TIERS) -> float:
    """Цена со скидкой, округлённая до копеек и не ниже `floor` (себестоимость + комиссия)."""
    price = round(base_price * (100 - discount_percent(completed_rentals, tiers)) / 100, 2)
    return max(price, floor)
