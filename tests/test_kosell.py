"""Тесты автопоставки KoSell в воркере (без сети: KoSell-клиент заменён заглушкой).
Проверяем: заказ привязанного лота → аренда+выдача, идемпотентность, !код/!время,
чужой чат игнорируется, окончание помечается."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rentbot.kosell_service as ks
from rentbot.adapters.fakes import FakeMarket, FakeNotifier
from rentbot.adapters.kosell import KoSellError
from rentbot.kosell_service import KoSellFulfiller
from rentbot.ports import ChatMessage, OrderEvent
from rentbot.storage import Storage
from rentbot.vault import Vault


async def _nosleep(*a, **k):  # мгновенные ретраи в тестах
    return None


ks.asyncio.sleep = _nosleep


class StubKoSell:
    """Заглушка KoSellClient: ничего не тратит, возвращает каноничные ответы."""
    def __init__(self):
        self.rented = []

    def rent(self, product_id, hours, currency, idempotency_key=None):
        self.rented.append(idempotency_key)
        return {"rental_uid": "UID-1", "product_name": "Raft", "steam_login": "LoGiN",
                "started_at": "2026-10-12T00:00:00Z", "expires_at": "2026-10-13T00:00:00Z",
                "status": "active"}

    def credentials(self, uid):
        return {"steam_login": "LoGiN", "steam_password": "PaSsW0rd"}

    def code(self, uid):
        return {"success": True, "code": "ABCDE", "expires_in": 20}


class NoStockKoSell(StubKoSell):
    """Заглушка: первые `fail` вызовов rent бросают no_stock, дальше — успех."""
    def __init__(self, fail=99):
        super().__init__()
        self.fail = fail
        self.calls = 0

    def rent(self, product_id, hours, currency, idempotency_key=None):
        self.calls += 1
        if self.calls <= self.fail:
            raise KoSellError(400, "no_stock", "нет свободных аккаунтов")
        return super().rent(product_id, hours, currency, idempotency_key)


def _fixture(client=None, retries=2):
    import tempfile
    dbp = os.path.join(tempfile.mkdtemp(), "r.db")
    s = Storage(dbp, Vault("pw", Vault.new_salt()))
    s.set_setting("kosell_key", "kosell_dummy", secret=True)
    s.kosell_map_set("playerok", "LOT-RAFT", 77, "Raft", 24, 150.0, "RUB", True)
    mk, note = FakeMarket(), FakeNotifier()
    f = KoSellFulfiller(s, mk, note, key="kosell_dummy", currency="RUB", retries=retries)
    f.client = client or StubKoSell()
    return s, mk, note, f


def _order(lot="LOT-RAFT", deal="DEAL1", chat="CHAT1"):
    return OrderEvent(marketplace="playerok", deal_id=deal, lot_id=lot,
                      buyer_id="B1", buyer_name="Петя", chat_id=chat, price=99.0)


def run(c):
    return asyncio.run(c)


def test_order_rents_and_delivers():
    s, mk, note, f = _fixture()
    assert run(f.handle_order(_order())) is True
    assert any("PaSsW0rd" in t for _, t in mk.sent), "креды не выданы покупателю"
    assert s.kosell_rental_by_deal("playerok", "DEAL1")["rental_uid"] == "UID-1"
    assert f.client.rented == ["DEAL1"], "idempotency_key != deal_id"
    print("PASS test_order_rents_and_delivers")


def test_unmapped_lot_ignored():
    s, mk, note, f = _fixture()
    assert run(f.handle_order(_order(lot="NOT-MAPPED"))) is False
    assert mk.sent == [], "по непривязанному лоту ничего не шлём"
    print("PASS test_unmapped_lot_ignored")


def test_idempotent_no_double_rent():
    s, mk, note, f = _fixture()
    run(f.handle_order(_order()))
    n = len(s.kosell_rental_list())
    run(f.handle_order(_order()))  # тот же заказ ещё раз
    assert len(s.kosell_rental_list()) == n, "создан дубль аренды"
    assert f.client.rented == ["DEAL1"], "повторная аренда у KoSell"
    print("PASS test_idempotent_no_double_rent")


def test_code_command():
    s, mk, note, f = _fixture()
    run(f.handle_order(_order()))
    assert run(f.handle_command(ChatMessage("playerok", "CHAT1", "B1", "!код"))) is True
    assert any("ABCDE" in t for _, t in mk.sent), "код не отправлен"
    print("PASS test_code_command")


def test_time_command():
    s, mk, note, f = _fixture()
    run(f.handle_order(_order()))
    assert run(f.handle_command(ChatMessage("playerok", "CHAT1", "B1", "!время"))) is True
    print("PASS test_time_command")


def test_foreign_chat_not_handled():
    s, mk, note, f = _fixture()
    run(f.handle_order(_order()))
    # чат без аренды KoSell → False, чтобы сообщение ушло обычному маршруту воркера
    assert run(f.handle_command(ChatMessage("playerok", "OTHER", "B9", "!код"))) is False
    print("PASS test_foreign_chat_not_handled")


def test_blacklisted_buyer_blocked():
    s, mk, note, f = _fixture()
    s.blacklist_add("playerok", "B1", "скам", "local")  # buyer_id из _order = B1
    assert run(f.handle_order(_order())) is True
    assert s.kosell_rental_by_deal("playerok", "DEAL1") is None, "скамеру выдали аренду"
    assert f.client.rented == [], "аренда у KoSell не должна браться"
    assert any("чёрном списке" in m for m in note.messages)
    print("PASS test_blacklisted_buyer_blocked")


def test_no_stock_opens_warranty():
    s, mk, note, f = _fixture(client=NoStockKoSell(fail=99), retries=1)
    assert run(f.handle_order(_order())) is True
    w = s.warranty_list()
    assert len(w) == 1 and w[0]["reason"] == "no_stock" and w[0]["status"] == "open"
    assert s.kosell_rental_by_deal("playerok", "DEAL1") is None, "аренда не должна записаться"
    assert any("нет свободного" in t for _, t in mk.sent), "покупателю не отправлено извинение"
    print("PASS test_no_stock_opens_warranty")


def test_no_stock_retry_then_success():
    stub = NoStockKoSell(fail=1)  # 1-й вызов no_stock, 2-й — успех
    s, mk, note, f = _fixture(client=stub, retries=2)
    assert run(f.handle_order(_order())) is True
    assert stub.calls == 2, "не было повтора после no_stock"
    assert s.kosell_rental_by_deal("playerok", "DEAL1")["rental_uid"] == "UID-1"
    assert s.warranty_list() == [], "успешный повтор не должен открывать гарантийный случай"
    print("PASS test_no_stock_retry_then_success")


def test_replace_command():
    s, mk, note, f = _fixture()
    run(f.handle_order(_order()))
    assert run(f.handle_command(ChatMessage("playerok", "CHAT1", "B1", "!замена"))) is True
    assert any("PaSsW0rd" in t for _, t in mk.sent), "доступ не перевыдан"
    w = s.warranty_list()
    assert len(w) == 1 and w[0]["reason"] == "buyer_replace"
    print("PASS test_replace_command")


def test_expiry_marks_expired():
    s, mk, note, f = _fixture()
    run(f.handle_order(_order()))
    s.kosell_rental_update_status("UID-1", "active", "2000-01-01T00:00:00Z")
    run(f.expiry_tick())
    assert s.kosell_rental_list()[0]["status"] == "expired"
    assert any("истекла" in m for m in note.messages)
    print("PASS test_expiry_marks_expired")


if __name__ == "__main__":
    import inspect
    tests = [fn for n, fn in sorted(globals().items())
             if n.startswith("test_") and inspect.isfunction(fn)]
    for t in tests:
        t()
    print(f"\n{len(tests)}/{len(tests)} passed")
