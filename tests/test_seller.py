"""Тесты базовых функций площадки: автовыдача, автоответчик, автоподнятие, Telegram.
Без сети, на фейковых портах и in-memory SQLite."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.adapters.fakes import FakeMarket, FakeNotifier
from rentbot.notify import TelegramNotifier
from rentbot.ports import ChatMessage, OrderEvent
from rentbot.services import AutoDelivery, AutoResponder, Bumper
from rentbot.storage import Storage
from rentbot.vault import Vault


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t
    def __call__(self):
        return self.t
    def advance(self, sec):
        self.t += sec


def mkstore():
    return Storage(":memory:", Vault("master", Vault.new_salt()))


def order(deal="D1", lot="LOT1", chat="C1", price=150.0):
    return OrderEvent("pl", deal, lot, "B1", "Вася", chat, price)


def run(coro):
    return asyncio.run(coro)


# ---------- автовыдача ----------
def test_delivery_text():
    s = mkstore(); mk = FakeMarket(); note = FakeNotifier()
    s.set_delivery("pl", "LOT1", "text", "Спасибо, {buyer}! Ваш товар: КОД-123")
    d = AutoDelivery(s, mk, note)
    r = run(d.deliver(order()))
    assert r["delivered"] and mk.sent[-1] == ("C1", "Спасибо, Вася! Ваш товар: КОД-123")
    assert any("Выдан товар" in m for m in note.messages)
    print("PASS test_delivery_text")


def test_delivery_idempotent():
    s = mkstore(); mk = FakeMarket(); note = FakeNotifier()
    s.set_delivery("pl", "LOT1", "text", "КОД")
    d = AutoDelivery(s, mk, note)
    assert run(d.deliver(order()))["delivered"]
    assert run(d.deliver(order()))["delivered"] is False  # повтор той же сделки
    assert len(mk.sent) == 1, "товар не должен выдаваться дважды"
    print("PASS test_delivery_idempotent")


def test_delivery_keys_pool_and_exhaustion():
    s = mkstore(); mk = FakeMarket(); note = FakeNotifier()
    s.set_delivery("pl", "LOT1", "keys")
    assert s.add_stock("pl", "LOT1", ["login1:pass1", "login2:pass2"]) == 2
    d = AutoDelivery(s, mk, note)
    assert run(d.deliver(order(deal="D1")))["delivered"]
    assert run(d.deliver(order(deal="D2")))["delivered"]
    assert mk.sent[0][1] == "login1:pass1" and mk.sent[1][1] == "login2:pass2"
    assert s.stock_count("pl", "LOT1") == 0
    # третий заказ — склад пуст: покупателю вежливо, владельцу тревога, выдача НЕ зафиксирована
    r = run(d.deliver(order(deal="D3")))
    assert r["delivered"] is False and r["reason"] == "склад пуст"
    assert any("ПУСТ" in m for m in note.messages)
    # пополнили — тот же заказ D3 теперь выдаётся (идемпотентность не съела слот)
    s.add_stock("pl", "LOT1", ["login3:pass3"])
    assert run(d.deliver(order(deal="D3")))["delivered"]
    assert mk.sent[-1][1] == "login3:pass3"
    print("PASS test_delivery_keys_pool_and_exhaustion")


def test_delivery_file():
    s = mkstore(); mk = FakeMarket(); note = FakeNotifier()
    s.set_delivery("pl", "LOT1", "file", "C:/goods/acc.txt")
    assert run(AutoDelivery(s, mk, note).deliver(order()))["delivered"]
    assert mk.files[-1][0] == "C1" and mk.files[-1][1] == "C:/goods/acc.txt"
    print("PASS test_delivery_file")


def test_delivery_skips_unconfigured_lot():
    s = mkstore(); mk = FakeMarket(); note = FakeNotifier()
    r = run(AutoDelivery(s, mk, note).deliver(order(lot="RENT_LOT")))
    assert r["delivered"] is False and not mk.sent  # лот аренды/без настройки — не выдаём
    print("PASS test_delivery_skips_unconfigured_lot")


# ---------- автоответчик ----------
def test_greeting_once():
    s = mkstore(); mk = FakeMarket()
    s.add_autoresponse("greeting", "Здравствуйте, {buyer}! Я бот-продавец, отвечаю мгновенно.")
    ar = AutoResponder(s, mk)
    assert run(ar.greet("pl", "C1", order())) is True
    assert run(ar.greet("pl", "C1", order())) is False  # второй раз не приветствуем
    assert len(mk.sent) == 1 and "Здравствуйте, Вася" in mk.sent[0][1]
    print("PASS test_greeting_once")


def test_autoresponse_keyword_and_commands():
    s = mkstore(); mk = FakeMarket()
    s.add_autoresponse("keyword", "Да, аккаунт с Prime.", pattern="prime")
    s.add_autoresponse("keyword", "Оплата через Playerok.", pattern="оплат")
    ar = AutoResponder(s, mk)
    assert run(ar.respond(ChatMessage("pl", "C1", "B1", "а есть PRIME?"))) == "Да, аккаунт с Prime."
    assert run(ar.respond(ChatMessage("pl", "C1", "B1", "как оплатить?"))) == "Оплата через Playerok."
    assert run(ar.respond(ChatMessage("pl", "C1", "B1", "привет"))) is None  # нет правила
    assert run(ar.respond(ChatMessage("pl", "C1", "B1", "!код"))) is None    # команда аренды — не трогаем
    print("PASS test_autoresponse_keyword_and_commands")


# ---------- автоподнятие ----------
def test_bumper_respects_interval():
    s = mkstore(); mk = FakeMarket(); note = FakeNotifier(); clock = Clock()
    s.set_bump("pl", "LOT1", enabled=True, interval_sec=3600)
    s.set_bump("pl", "LOT2", enabled=False, interval_sec=3600)  # выключен — не трогаем
    b = Bumper(s, mk, note, clock=clock)
    assert run(b.tick()) == 1 and mk.bumped[-1][0] == "LOT1"
    assert run(b.tick()) == 0, "интервал не прошёл — второй раз не поднимаем"
    clock.advance(3601)
    assert run(b.tick()) == 1, "интервал прошёл — поднимаем снова"
    print("PASS test_bumper_respects_interval")


def test_bumper_failure_isolated():
    s = mkstore(); note = FakeNotifier(); clock = Clock()
    class BadMarket(FakeMarket):
        async def bump_item(self, lot_id, price):
            raise RuntimeError("cloudflare")
    s.set_bump("pl", "LOT1", enabled=True, interval_sec=0)
    b = Bumper(s, BadMarket(), note, clock=clock)
    assert run(b.tick()) == 0  # сбой не считается успехом
    assert any("Не удалось поднять" in m for m in note.messages)
    print("PASS test_bumper_failure_isolated")


# ---------- Telegram-уведомления ----------
def test_telegram_notifier_payload():
    captured = {}
    async def fake_transport(method, payload):
        captured["method"] = method; captured["payload"] = payload
        return {"ok": True}
    n = TelegramNotifier("TOKEN", "123456", transport=fake_transport)
    run(n.notify("Новый заказ #42"))
    assert captured["method"] == "sendMessage"
    assert captured["payload"]["chat_id"] == "123456"
    assert captured["payload"]["text"] == "Новый заказ #42"
    print("PASS test_telegram_notifier_payload")


def test_settings_secret_roundtrip():
    s = mkstore()
    s.set_setting("tg_token", "123:ABCsecret", secret=True)
    s.set_setting("tg_chat", "999")
    # в сыром виде токен зашифрован
    raw = s.db.execute("SELECT v FROM settings WHERE k='tg_token'").fetchone()["v"]
    assert raw.startswith("enc:") and "ABCsecret" not in raw
    assert s.get_setting("tg_token") == "123:ABCsecret"
    assert s.get_setting("tg_chat") == "999"
    print("PASS test_settings_secret_roundtrip")


if __name__ == "__main__":
    import inspect
    tests = [f for name, f in sorted(globals().items())
             if name.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for t in tests:
        t(); passed += 1
    print(f"\n{passed}/{len(tests)} passed")
