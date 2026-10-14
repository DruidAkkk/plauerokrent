"""Тесты движка аренды: один полный цикл, идемпотентность, очередь, продление,
карантин при сбое ротации, лимит кодов. Без сети, на фейковых портах."""
import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.adapters.fakes import FakeMarket, FakeNotifier, FakeSteam
from rentbot.domain import Account, AccountState, LotBinding, RentalState, RotationMode
from rentbot.engine import RentalEngine
from rentbot.ports import OrderEvent
from rentbot.storage import Storage
from rentbot.vault import Vault


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


def setup(n_accounts=1, rotation=RotationMode.AUTO, steam_fail=False,
          autokick=True, deauth_fail=False):
    vault = Vault("master", Vault.new_salt())
    s = Storage(":memory:", vault)
    # по умолчанию (как в продукте) конец аренды = авто-кик без смены пароля
    s.set_setting("steam_autokick", "1" if autokick else "0")
    for i in range(n_accounts):
        s.add_account(Account(None, "cs2", f"user{i}", f"pwd{i}",
                              shared_secret=base64.b64encode(os.urandom(20)).decode(),
                              identity_secret="id"))
    s.bind_lot(LotBinding("fake", "LOT1", "cs2", hours=2, rotation=rotation))
    clock = Clock()
    mk, steam, note = FakeMarket(), FakeSteam(fail=steam_fail, deauth_fail=deauth_fail), FakeNotifier()
    eng = RentalEngine(s, mk, steam, note, clock=clock)
    return s, eng, mk, steam, note, clock


def order(deal="D1", buyer="B1", chat="C1", price=150.0):
    return OrderEvent("fake", deal, "LOT1", buyer, "Вася", chat, price)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_full_cycle():
    s, eng, mk, steam, note, clock = setup()
    run(eng.on_order(order()))
    assert "Логин: user0" in mk.last("C1")
    assert s.get_account(1).state == AccountState.RENTED

    # код Guard по запросу
    run(eng.on_chat("fake", "C1", "B1", "!код"))
    assert "Код Steam Guard" in mk.last("C1")

    # время вышло → авто-кик (без смены пароля) → отдых → свободен
    clock.advance(2 * 3600 + 1)
    run(eng.tick())
    assert steam.deauths == ["user0"]                  # вышли со всех устройств
    assert steam.rotations == 0                        # пароль НЕ меняли
    assert s.get_account(1).state == AccountState.RESTING
    assert s.get_account(1).password == "pwd0"         # пароль прежний
    clock.advance(10 * 60)
    run(eng.tick())
    assert s.get_account(1).state == AccountState.FREE


def test_idempotent_order():
    s, eng, mk, *_ = setup()
    run(eng.on_order(order(deal="D1")))
    run(eng.on_order(order(deal="D1")))            # тот же deal_id
    assert len([m for m in mk.sent if "Логин" in m[1]]) == 1


def test_queue_then_assign():
    s, eng, mk, steam, note, clock = setup(n_accounts=1)
    run(eng.on_order(order(deal="D1", buyer="B1", chat="C1")))     # занял аккаунт
    run(eng.on_order(order(deal="D2", buyer="B2", chat="C2")))     # в очередь
    assert "очеред" in mk.last("C2").lower()
    # первый закончил → второй получает аккаунт автоматически
    clock.advance(2 * 3600 + 1)
    run(eng.tick())                 # завершение + ротация → resting
    clock.advance(10 * 60)
    run(eng.tick())                 # отдых прошёл → выдача из очереди
    assert "Логин: user0" in mk.last("C2")


def test_extend():
    s, eng, mk, steam, note, clock = setup()
    run(eng.on_order(order(deal="D1", buyer="B1", chat="C1")))
    r = s.rentals_by_state(RentalState.ACTIVE)[0]
    ends1 = r.ends_at
    run(eng.on_order(order(deal="D2", buyer="B1", chat="C1")))     # тот же покупатель
    r2 = s.rentals_by_state(RentalState.ACTIVE)[0]
    assert abs((r2.ends_at - ends1) - 2 * 3600) < 1
    assert "продлена" in mk.last("C1").lower()


def test_rotation_failure_quarantine():
    # авто-кик выключен → используется ротация; ротация падает → карантин
    s, eng, mk, steam, note, clock = setup(steam_fail=True, autokick=False)
    run(eng.on_order(order()))
    clock.advance(2 * 3600 + 1)
    run(eng.tick())
    assert steam.rotations == 3                      # 3 попытки
    assert s.get_account(1).state == AccountState.QUARANTINE
    assert any("Карантин" in m for m in note.messages)


def test_autokick_disabled_uses_rotation():
    # авто-кик выключен → конец аренды меняет пароль (старая схема)
    s, eng, mk, steam, note, clock = setup(autokick=False)
    run(eng.on_order(order()))
    clock.advance(2 * 3600 + 1)
    run(eng.tick())
    assert steam.deauths == []                       # кика не было
    assert steam.rotations == 1
    assert s.get_account(1).state == AccountState.RESTING
    assert s.get_account(1).password == "newpwd1"    # пароль сменился


def test_autokick_fail_falls_back_to_rotation():
    # авто-кик включён, но упал → подстраховка ротацией (аккаунт не остаётся открытым)
    s, eng, mk, steam, note, clock = setup(autokick=True, deauth_fail=True)
    run(eng.on_order(order()))
    clock.advance(2 * 3600 + 1)
    run(eng.tick())
    assert steam.deauths == ["user0"]                # кик пытались
    assert steam.rotations == 1                      # и упали в ротацию
    assert s.get_account(1).state == AccountState.RESTING
    assert any("Авто-кик не удался" in m for m in note.messages)


def test_blacklisted_buyer_blocked():
    s, eng, mk, steam, note, clock = setup()
    s.blacklist_add("fake", "B1", "скам", "local")   # buyer_id из order() = B1
    run(eng.on_order(order()))
    assert s.get_account(1).state == AccountState.FREE, "скамеру не должны выдавать аккаунт"
    assert not any("Логин" in t for _, t in mk.sent), "скамеру не должен уйти доступ"
    assert any("ЧС" in m for m in note.messages), "владелец не уведомлён о блоке"
    print("PASS test_blacklisted_buyer_blocked")


def test_code_only_during_rent():
    s, eng, mk, *_ = setup()
    run(eng.on_chat("fake", "CX", "BX", "!код"))      # аренды нет
    assert "активной аренды" in mk.last("CX")


def test_lot_visibility_sync():
    s, eng, mk, steam, note, clock = setup(n_accounts=1)
    run(eng.sync_lot_visibility())
    assert mk.lot_active["LOT1"] is True
    run(eng.on_order(order()))                        # пул опустел
    run(eng.sync_lot_visibility())
    assert mk.lot_active["LOT1"] is False


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            ok += 1
        except Exception:
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{ok}/{len(tests)} passed")
    sys.exit(0 if ok == len(tests) else 1)
