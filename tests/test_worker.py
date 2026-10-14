"""Тесты маршрутизации live-воркера: куда уходит оплата и сообщение.
Без сети: вызываем on_order/on_chat напрямую с фейковыми портами."""
import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.adapters.fakes import FakeMarket, FakeNotifier, FakeSteam
from rentbot.domain import Account, LotBinding, RotationMode
from rentbot.engine import RentalEngine
from rentbot.ports import ChatMessage, OrderEvent
from rentbot.services import AutoDelivery, AutoResponder, Bumper
from rentbot.storage import Storage
from rentbot.vault import Vault
from rentbot.worker import HumanDelayMarket, Worker


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t
    def __call__(self):
        return self.t
    def advance(self, s):
        self.t += s


def run(c):
    return asyncio.run(c)


def build():
    s = Storage(":memory:", Vault("m", Vault.new_salt()))
    clock = Clock()
    mk, steam, note = FakeMarket(), FakeSteam(), FakeNotifier()
    eng = RentalEngine(s, mk, steam, note, clock=clock)
    deliv = AutoDelivery(s, mk, note, clock=clock)
    resp = AutoResponder(s, mk, clock=clock)
    bump = Bumper(s, mk, note, clock=clock)
    w = Worker(s, None, eng, deliv, resp, bump, note)
    return s, w, mk, note, clock


def order(deal="D1", lot="L", chat="C1", price=150.0):
    return OrderEvent("fake", deal, lot, "B1", "Вася", chat, price)


def test_order_rental_lot_goes_to_engine():
    s, w, mk, note, clock = build()
    s.add_account(Account(None, "cs2", "u0", "pwd0",
                          shared_secret=base64.b64encode(os.urandom(20)).decode()))
    s.bind_lot(LotBinding("fake", "RENT", "cs2", hours=2, rotation=RotationMode.MANUAL))
    run(w.on_order(order(lot="RENT")))
    assert any("Аренда началась" in t for _, t in mk.sent), "движок должен выдать аккаунт"
    print("PASS test_order_rental_lot_goes_to_engine")


def test_order_delivery_lot_goes_to_autodelivery():
    s, w, mk, note, clock = build()
    s.set_delivery("fake", "SHOP", "text", "Ваш ключ: ABC-{price}")
    run(w.on_order(order(lot="SHOP")))
    assert mk.sent[-1] == ("C1", "Ваш ключ: ABC-150.0")
    print("PASS test_order_delivery_lot_goes_to_autodelivery")


def test_order_unmanaged_lot_notifies_owner_only():
    s, w, mk, note, clock = build()
    run(w.on_order(order(lot="UNKNOWN")))
    assert not mk.sent, "покупателю ничего не пишем для чужого лота"
    assert any("Новый заказ" in m and "не настроен" in m for m in note.messages)
    print("PASS test_order_unmanaged_lot_notifies_owner_only")


def test_chat_command_goes_to_engine():
    s, w, mk, note, clock = build()
    # поднимаем активную аренду через движок
    s.add_account(Account(None, "cs2", "u0", "pwd0",
                          shared_secret=base64.b64encode(os.urandom(20)).decode()))
    s.bind_lot(LotBinding("fake", "RENT", "cs2", hours=2, rotation=RotationMode.MANUAL))
    run(w.on_order(order(lot="RENT", chat="C1")))
    mk.sent.clear()
    run(w.on_chat(ChatMessage("fake", "C1", "B1", "!код")))
    assert any("Код Steam Guard" in t for _, t in mk.sent), "команда !код → движок"
    print("PASS test_chat_command_goes_to_engine")


def test_chat_question_greets_and_autoresponds():
    s, w, mk, note, clock = build()
    s.add_autoresponse("greeting", "Здравствуйте! Отвечаю быстро.")
    s.add_autoresponse("keyword", "Да, с Prime.", pattern="prime")
    run(w.on_chat(ChatMessage("fake", "C1", "B1", "а есть prime?")))
    texts = [t for _, t in mk.sent]
    assert "Здравствуйте! Отвечаю быстро." in texts, "первое сообщение → приветствие"
    assert "Да, с Prime." in texts, "ключевое слово → автоответ"
    assert any("Сообщение" in m for m in note.messages), "владельцу — уведомление"
    # второй раз приветствие не повторяется
    mk.sent.clear()
    run(w.on_chat(ChatMessage("fake", "C1", "B1", "ещё раз про prime")))
    assert "Здравствуйте! Отвечаю быстро." not in [t for _, t in mk.sent]
    print("PASS test_chat_question_greets_and_autoresponds")


def test_human_delay_market_delegates():
    mk = FakeMarket()
    hm = HumanDelayMarket(mk, lo=0, hi=0)  # без задержки в тесте
    run(hm.send_message("C1", "hi"))
    run(hm.bump_item("L", 10.0))
    assert mk.sent == [("C1", "hi")] and mk.bumped == [("L", 10.0)]
    print("PASS test_human_delay_market_delegates")


if __name__ == "__main__":
    import inspect
    tests = [f for n, f in sorted(globals().items())
             if n.startswith("test_") and inspect.isfunction(f)]
    for t in tests:
        t()
    print(f"\n{len(tests)}/{len(tests)} passed")
