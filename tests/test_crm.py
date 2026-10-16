"""Тесты CRM покупателей (агрегация по Steam-арендам)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.domain import Rental, RentalState
from rentbot.storage import Storage
from rentbot.vault import Vault


def _storage():
    dbp = os.path.join(tempfile.mkdtemp(), "r.db")
    return Storage(dbp, Vault("pw", Vault.new_salt()))


def _rent(s, deal, buyer, name, state, started, price, account_id=1):
    s.insert_rental(Rental(None, "playerok", deal, "LOT1", buyer, name, "C" + deal,
                           account_id, state, started, started + 7200, price))


def test_crm_aggregates_deals_and_ltv():
    s = _storage()
    # Вася: две завершённые аренды
    _rent(s, "D1", "B1", "Вася", RentalState.ENDED, 1000.0, 150.0)
    _rent(s, "D2", "B1", "Вася", RentalState.ENDED, 2000.0, 200.0)
    # Петя: активная + продление (CANCELLED, но с account_id — считается сделкой)
    _rent(s, "D3", "B2", "Петя", RentalState.ACTIVE, 1500.0, 99.0)
    _rent(s, "D4", "B2", "Петя", RentalState.CANCELLED, 1600.0, 99.0, account_id=1)
    # Скам: блок по ЧС (CANCELLED + account_id IS NULL) — НЕ покупатель
    _rent(s, "D5", "B3", "Скам", RentalState.CANCELLED, 1700.0, 0.0, account_id=None)

    items = s.buyers_crm("playerok")
    assert [b["buyer_id"] for b in items] == ["B1", "B2"], "скам-блок не должен попадать, сортировка по LTV"
    b1 = items[0]
    assert b1["buyer_name"] == "Вася" and b1["deals"] == 2 and b1["ltv"] == 350.0
    assert b1["first_seen"] == 1000.0 and b1["last_seen"] == 2000.0
    b2 = items[1]
    assert b2["deals"] == 2 and b2["ltv"] == 198.0, "продление считается сделкой и учитывается в LTV"
    print("PASS test_crm_aggregates_deals_and_ltv")


def test_crm_empty():
    s = _storage()
    assert s.buyers_crm("playerok") == []
    print("PASS test_crm_empty")


def test_crm_only_blacklist_blocks_is_empty():
    s = _storage()
    _rent(s, "D1", "B3", "Скам", RentalState.CANCELLED, 1700.0, 0.0, account_id=None)
    assert s.buyers_crm("playerok") == [], "один лишь блок по ЧС — не покупатель"
    print("PASS test_crm_only_blacklist_blocks_is_empty")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in tests:
        f()
    print(f"{len(tests)}/{len(tests)} passed")
