"""Тест окна статистики (на нём держится переключатель периода неделя/месяц на дашборде)."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.domain import Rental, RentalState
from rentbot.storage import Storage
from rentbot.vault import Vault

WEEK = 7 * 24 * 3600
MONTH = 30 * 24 * 3600


def _storage():
    dbp = os.path.join(tempfile.mkdtemp(), "r.db")
    return Storage(dbp, Vault("pw", Vault.new_salt()))


def test_period_window_week_vs_month():
    s = _storage()
    now = time.time()
    # аренда 10 дней назад: входит в «месяц», но НЕ в «неделю»
    s.insert_rental(Rental(None, "playerok", "OLD", "LOT1", "B1", "Вася", "C1", 1,
                           RentalState.ENDED, now - 10 * 24 * 3600, now - 10 * 24 * 3600 + 7200, 200.0))
    # аренда 2 дня назад: входит в оба окна
    s.insert_rental(Rental(None, "playerok", "NEW", "LOT1", "B2", "Петя", "C2", 1,
                           RentalState.ENDED, now - 2 * 24 * 3600, now - 2 * 24 * 3600 + 7200, 150.0))

    week = s.stats(since=now - WEEK)
    month = s.stats(since=now - MONTH)
    assert week["rentals"] == 1 and week["revenue"] == 150.0, "в неделю попадает только свежая"
    assert month["rentals"] == 2 and month["revenue"] == 350.0, "в месяц — обе"
    print("PASS test_period_window_week_vs_month")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in tests:
        f()
    print(f"{len(tests)}/{len(tests)} passed")
