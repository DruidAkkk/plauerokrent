"""Тесты PUBG Mobile аренды в воркере (без сети: IMAP-чтение кода заменено заглушкой).
Проверяем: заказ → выдача свободного аккаунта, нет свободных → гарантия, !код/!время,
окончание освобождает аккаунт, идемпотентность, чужой/непривязанный лот."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rentbot.mobile_otp as mobile_otp
from rentbot.adapters.fakes import FakeMarket, FakeNotifier
from rentbot.mobile_service import MobileFulfiller
from rentbot.ports import ChatMessage, OrderEvent
from rentbot.storage import Storage
from rentbot.vault import Vault

# заглушка чтения кода с почты (без реального IMAP)
mobile_otp.read_otp = lambda *a, **k: ("246813", "stub")


def _fixture(accounts=1, email=True):
    import tempfile
    dbp = os.path.join(tempfile.mkdtemp(), "r.db")
    s = Storage(dbp, Vault("pw", Vault.new_salt()))
    for i in range(accounts):
        s.mobile_add(game="pubg_mobile", login_type="facebook", login=f"acc{i}@mail.com",
                     password=f"pass{i}", email=("box@gmail.com" if email else None),
                     email_password=("imap" if email else None),
                     imap_host="imap.gmail.com", proxy=None, note="", tags=[])
    s.mobile_lot_set("playerok", "LOT-PUBG", "pubg_mobile", 24, True)
    mk, note = FakeMarket(), FakeNotifier()
    return s, mk, note, MobileFulfiller(s, mk, note)


def _order(lot="LOT-PUBG", deal="D1", chat="C1"):
    return OrderEvent(marketplace="playerok", deal_id=deal, lot_id=lot,
                      buyer_id="B1", buyer_name="Вася", chat_id=chat, price=199.0)


def run(c):
    return asyncio.run(c)


def test_order_delivers_and_marks_rented():
    s, mk, note, f = _fixture()
    assert run(f.handle_order(_order())) is True
    assert any("pass0" in t for _, t in mk.sent), "пароль не выдан"
    assert s.mobile_list()[0]["state"] == "rented", "аккаунт не помечен в аренде"
    assert s.mobile_rental_by_deal("playerok", "D1")["account_id"] == s.mobile_list()[0]["id"]
    print("PASS test_order_delivers_and_marks_rented")


def test_unmapped_lot_ignored():
    s, mk, note, f = _fixture()
    assert run(f.handle_order(_order(lot="OTHER"))) is False
    assert mk.sent == []
    print("PASS test_unmapped_lot_ignored")


def test_no_free_opens_warranty():
    s, mk, note, f = _fixture(accounts=1)
    run(f.handle_order(_order(deal="D1", chat="C1")))        # занял единственный
    run(f.handle_order(_order(deal="D2", chat="C2")))        # второй заказ — свободных нет
    w = s.warranty_list()
    assert any(x["reason"] == "no_stock" for x in w), "гарантийный случай не открыт"
    assert s.mobile_rental_by_deal("playerok", "D2") is None
    assert any("ближайшее время" in t for c, t in mk.sent if c == "C2")
    print("PASS test_no_free_opens_warranty")


def test_idempotent_no_double_issue():
    s, mk, note, f = _fixture(accounts=2)
    run(f.handle_order(_order()))
    n = len(s.mobile_rental_list())
    run(f.handle_order(_order()))  # тот же заказ
    assert len(s.mobile_rental_list()) == n, "создана вторая аренда по тому же заказу"
    print("PASS test_idempotent_no_double_issue")


def test_code_command_reads_otp():
    s, mk, note, f = _fixture(email=True)
    run(f.handle_order(_order()))
    assert run(f.handle_command(ChatMessage("playerok", "C1", "B1", "!код"))) is True
    assert any("246813" in t for _, t in mk.sent), "код с почты не отправлен"
    print("PASS test_code_command_reads_otp")


def test_code_unconfigured_email():
    s, mk, note, f = _fixture(email=False)
    run(f.handle_order(_order()))
    assert run(f.handle_command(ChatMessage("playerok", "C1", "B1", "!код"))) is True
    assert any("не настроен" in t for _, t in mk.sent)
    print("PASS test_code_unconfigured_email")


def test_time_command():
    s, mk, note, f = _fixture()
    run(f.handle_order(_order()))
    assert run(f.handle_command(ChatMessage("playerok", "C1", "B1", "!время"))) is True
    print("PASS test_time_command")


def test_foreign_chat_not_handled():
    s, mk, note, f = _fixture()
    run(f.handle_order(_order()))
    assert run(f.handle_command(ChatMessage("playerok", "OTHER", "B9", "!код"))) is False
    print("PASS test_foreign_chat_not_handled")


def test_blacklisted_buyer_blocked():
    s, mk, note, f = _fixture(accounts=1)
    s.blacklist_add("mobile", "playerok", "B1", "скам")  # ЧС PUBG Mobile; buyer_id из _order = B1
    assert run(f.handle_order(_order())) is True
    assert s.mobile_rental_by_deal("playerok", "D1") is None, "скамеру выдали аккаунт"
    assert s.mobile_list()[0]["state"] == "free", "аккаунт не должен уходить в аренду"
    assert any("чёрном списке" in m for m in note.messages)
    print("PASS test_blacklisted_buyer_blocked")


def test_expiry_frees_account():
    s, mk, note, f = _fixture()
    run(f.handle_order(_order()))
    rid = s.mobile_rental_list()[0]["id"]
    # просрочим аренду
    s.db.execute("UPDATE mobile_rentals SET expires=? WHERE id=?", (time.time() - 10, rid))
    run(f.expiry_tick())
    assert s.mobile_list()[0]["state"] == "free", "аккаунт не освобождён"
    assert s.mobile_rental_list()[0]["status"] == "expired"
    print("PASS test_expiry_frees_account")


def _expire(s):
    rid = s.mobile_rental_list()[0]["id"]
    s.db.execute("UPDATE mobile_rentals SET expires=? WHERE id=?", (time.time() - 10, rid))


def test_expiry_autokick_disabled_by_default():
    s, mk, note, f = _fixture()
    run(f.handle_order(_order()))
    _expire(s)
    import rentbot.mobile_kick as mk_mod
    called = []

    async def spy(acc):
        called.append(acc)
        return {"ok": True}

    orig, mk_mod.kick = mk_mod.kick, spy
    try:
        run(f.expiry_tick())
    finally:
        mk_mod.kick = orig
    assert called == [], "по умолчанию авто-кик не должен вызываться"
    assert s.mobile_list()[0]["state"] == "free"
    assert any("сменить доступ" in m for m in note.messages)
    print("PASS test_expiry_autokick_disabled_by_default")


def test_expiry_autokick_enabled_calls_kick():
    s, mk, note, f = _fixture()
    s.set_setting("mobile_autokick", "1")
    run(f.handle_order(_order()))
    login = s.mobile_list()[0]["login"]
    _expire(s)
    import rentbot.mobile_kick as mk_mod
    seen = []

    async def ok_kick(acc):
        seen.append(acc["login"])
        return {"ok": True, "message": "ok"}

    orig, mk_mod.kick = mk_mod.kick, ok_kick
    try:
        run(f.expiry_tick())
    finally:
        mk_mod.kick = orig
    assert seen == [login], "авто-кик не вызван для аккаунта"
    assert s.mobile_list()[0]["state"] == "free"
    assert any("Авто-кик выполнен" in m for m in note.messages)
    print("PASS test_expiry_autokick_enabled_calls_kick")


def test_expiry_autokick_unsupported_still_frees():
    s, mk, note, f = _fixture()
    s.set_setting("mobile_autokick", "1")
    run(f.handle_order(_order()))
    _expire(s)
    import rentbot.mobile_kick as mk_mod

    async def no_kick(acc):
        return {"ok": False, "message": "не автоматизирован"}

    orig, mk_mod.kick = mk_mod.kick, no_kick
    try:
        run(f.expiry_tick())
    finally:
        mk_mod.kick = orig
    assert s.mobile_list()[0]["state"] == "free", "аккаунт должен освободиться даже если кик не вышел"
    assert s.mobile_rental_list()[0]["status"] == "expired"
    assert any("не автоматизирован" in m for m in note.messages)
    print("PASS test_expiry_autokick_unsupported_still_frees")


def test_blacklist_is_per_rental_scope():
    s, mk, note, f = _fixture()
    # у каждой аренды свой независимый ЧС: блок в одной не влияет на другие
    s.blacklist_add("mobile", "playerok", "X1", "скам")
    assert s.is_blacklisted("mobile", "playerok", "X1") is True, "в своём scope должен блокировать"
    assert s.is_blacklisted("steam", "playerok", "X1") is False, "в чужом scope блока быть не должно"
    assert s.is_blacklisted("kosell", "playerok", "X1") is False, "в чужом scope блока быть не должно"
    # список и удаление тоже по scope
    assert [r["buyer_id"] for r in s.blacklist_list("mobile")] == ["X1"]
    assert s.blacklist_list("steam") == []
    s.blacklist_remove("mobile", "playerok", "X1")
    assert s.is_blacklisted("mobile", "playerok", "X1") is False
    print("PASS test_blacklist_is_per_rental_scope")


if __name__ == "__main__":
    import inspect
    tests = [fn for n, fn in sorted(globals().items())
             if n.startswith("test_") and inspect.isfunction(fn)]
    for t in tests:
        t()
    print(f"\n{len(tests)}/{len(tests)} passed")
