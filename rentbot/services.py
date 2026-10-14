"""Базовые функции площадки (бесплатный слой, не про аренду):

  • AutoDelivery  — автовыдача товара после оплаты: текст-шаблон, одноразовый ключ со склада,
                    или файл (логин-пароль). Идемпотентно по deal_id.
  • AutoResponder — приветствие нового покупателя + ответы по ключевым словам.
  • Bumper        — автоподнятие/перевыставление лотов по интервалу (по умолчанию выключено).

Всё чистое и тестируемое: зависит только от Storage + портов (Marketplace, Notifier),
сеть не трогает. Человеческие задержки/антибан — в оркестраторе-воркере, не здесь
(интервал автоподнятия — сам по себе антибан-ограничитель).
"""
from __future__ import annotations

import time

from .ports import ChatMessage, Marketplace, Notifier, OrderEvent
from .storage import Storage


def render(template: str, order: OrderEvent) -> str:
    """Подстановка плейсхолдеров в шаблон выдачи/ответа."""
    return (template
            .replace("{buyer}", order.buyer_name or "покупатель")
            .replace("{lot}", order.lot_id)
            .replace("{price}", str(order.price))
            .replace("{deal}", order.deal_id))


class AutoDelivery:
    """Выдаёт товар после оплаты. Для лотов аренды выдачи нет (там свой движок)."""

    def __init__(self, storage: Storage, market: Marketplace, notifier: Notifier, *, clock=time.time):
        self.s = storage
        self.mk = market
        self.note = notifier
        self.now = clock

    async def deliver(self, order: OrderEvent) -> dict:
        cfg = self.s.get_delivery(order.marketplace, order.lot_id)
        if not cfg or not cfg["enabled"]:
            return {"delivered": False, "reason": "нет автовыдачи для лота"}

        # идемпотентность: одна выдача на сделку
        if not self.s.mark_delivered(order.marketplace, order.deal_id, self.now()):
            return {"delivered": False, "reason": "уже выдано"}

        mode = cfg["mode"]
        if mode == "text":
            text = render(cfg["template"], order)
            await self.mk.send_message(order.chat_id, text)

        elif mode == "keys":
            key = self.s.claim_stock(order.marketplace, order.lot_id, order.deal_id, self.now())
            if key is None:
                # склад пуст — честно говорим покупателю и зовём владельца, выдачу не теряем
                self.s.db.execute("DELETE FROM delivered WHERE marketplace=? AND deal_id=?",
                                  (order.marketplace, order.deal_id))
                await self.mk.send_message(order.chat_id,
                    "Оплата получена. Товар выдам в течение нескольких минут — уже готовлю.")
                await self.note.notify(
                    f"⚠️ Склад ключей для лота {order.lot_id} ПУСТ — заказ {order.deal_id} ждёт выдачи!")
                self.s.log("delivery_out_of_stock", data={"deal": order.deal_id, "lot": order.lot_id})
                return {"delivered": False, "reason": "склад пуст"}
            await self.mk.send_message(order.chat_id, key)

        elif mode == "file":
            await self.mk.send_file(order.chat_id, cfg["template"],
                                    caption=f"{order.buyer_name}, спасибо за покупку!")
        else:
            return {"delivered": False, "reason": f"неизвестный режим {mode}"}

        self.s.log("delivered", data={"deal": order.deal_id, "lot": order.lot_id, "mode": mode})
        await self.note.notify(
            f"✅ Выдан товар по заказу {order.deal_id} (лот {order.lot_id}, {order.price}₽) "
            f"покупателю {order.buyer_name}")
        return {"delivered": True, "mode": mode}


class AutoResponder:
    """Приветствие нового покупателя + ответы по ключевым словам.
    Команды аренды (!код/!время) НЕ трогает — ими занимается движок аренды."""

    def __init__(self, storage: Storage, market: Marketplace, *, clock=time.time):
        self.s = storage
        self.mk = market
        self.now = clock

    async def greet(self, marketplace: str, chat_id: str, order: OrderEvent | None = None,
                    buyer_name: str | None = None) -> bool:
        """Поприветствовать, если этому чату ещё не писали приветствие."""
        if not self.s.mark_greeted(marketplace, chat_id, self.now()):
            return False
        rule = next((r for r in self.s.list_autoresponses()
                     if r["kind"] == "greeting" and r["enabled"]), None)
        if not rule:
            return False
        if order is not None:
            text = render(rule["reply"], order)
        else:
            text = rule["reply"].replace("{buyer}", buyer_name or "покупатель")
        await self.mk.send_message(chat_id, text)
        self.s.log("greeted", data={"chat": chat_id})
        return True

    async def respond(self, msg: ChatMessage) -> str | None:
        """Ответить на сообщение по первому подходящему правилу. None — если не ответили."""
        text = (msg.text or "").strip()
        if text.startswith("!"):
            return None  # команды аренды — не наше
        low = text.lower()
        for r in self.s.list_autoresponses():
            if r["kind"] != "keyword" or not r["enabled"] or not r["pattern"]:
                continue
            if r["pattern"].lower() in low:
                await self.mk.send_message(msg.chat_id, r["reply"])
                self.s.log("autoresponse", data={"chat": msg.chat_id, "pattern": r["pattern"]})
                return r["reply"]
        return None


class Bumper:
    """Автоподнятие/перевыставление лотов. По умолчанию выключено (антибан).
    Поднимает только те лоты, у которых enabled=1 и прошёл интервал."""

    def __init__(self, storage: Storage, market: Marketplace, notifier: Notifier, *, clock=time.time):
        self.s = storage
        self.mk = market
        self.note = notifier
        self.now = clock

    async def tick(self) -> int:
        """Поднять все лоты, которым пора. Возвращает число поднятых."""
        due = self.s.bump_due(self.now())
        bumped = 0
        for lot in due:
            try:
                await self.mk.bump_item(lot["lot_id"], 0.0)
                self.s.mark_bumped(lot["marketplace"], lot["lot_id"], self.now())
                self.s.log("bumped", data={"lot": lot["lot_id"]})
                bumped += 1
            except Exception as e:  # noqa: BLE001 — один сбойный лот не должен ронять остальные
                self.s.log("bump_fail", data={"lot": lot["lot_id"], "err": str(e)})
                await self.note.notify(f"⚠️ Не удалось поднять лот {lot['lot_id']}: {e}")
        return bumped
