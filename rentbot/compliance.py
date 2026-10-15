"""Предохранитель соответствия правилам площадки (#20).

Playerok запрещает создавать и удалять товары через автоматизацию (спам однотипных лотов),
но разрешает перевыставлять уже проданные/снятые. Модуль — чистая политика без сети:
`check()` классифицирует действие, `ActionBudget` ограничивает частоту (скользящее окно).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

# Разрешённые автоматизацией действия над СУЩЕСТВУЮЩИМИ лотами.
ALLOWED = frozenset({"relist", "activate", "deactivate", "bump", "reprice", "edit_text"})
# Запрещённые правилами Playerok при автоматизации.
FORBIDDEN = {
    "create_item": "создание новых товаров автоматизацией запрещено правилами Playerok",
    "delete_item": "удаление товаров автоматизацией запрещено правилами Playerok",
}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


def check(action: str) -> Verdict:
    if action in FORBIDDEN:
        return Verdict(False, FORBIDDEN[action])
    if action in ALLOWED:
        return Verdict(True)
    return Verdict(False, f"неизвестное действие «{action}» — по умолчанию запрещено")


class ActionBudget:
    """Не более `limit` действий за `window` секунд (защита от всплесков)."""

    def __init__(self, limit: int, window: float, clock):
        self.limit, self.window, self.clock = limit, window, clock
        self._ts: deque[float] = deque()

    def allow(self) -> bool:
        now = self.clock()
        while self._ts and now - self._ts[0] >= self.window:
            self._ts.popleft()
        if len(self._ts) >= self.limit:
            return False
        self._ts.append(now)
        return True
