"""Автовыдача и возврат PUBG Mobile аренды для live-воркера.

При заказе привязанного лота: берём свободный аккаунт из пула, отдаём доступ покупателю,
помечаем «в аренде», ставим таймер. !код — читаем одноразовый код с подконтрольной почты
(IMAP) и шлём покупателю. По окончании срока освобождаем аккаунт.

Автовозврат здесь = освобождение аккаунта под следующую аренду (+ напоминание владельцу сменить
доступ). Смена пароля соц-логина зависит от провайдера и делается владельцем вручную.
"""
from __future__ import annotations

import asyncio
import time

from . import mobile_otp, review_request
from .ports import ChatMessage, Marketplace, Notifier, OrderEvent
from .storage import Storage

LOGIN_LABEL = {"facebook": "Facebook", "google": "Google", "twitter": "Twitter/X",
               "vk": "VK", "apple": "Apple", "email": "Почта/телефон"}
CMD_CODE = ("!код", "!code")
CMD_TIME = ("!время", "!time")


def _fmt_left(expires_epoch: float) -> str:
    sec = expires_epoch - time.time()
    if sec <= 0:
        return "истекла"
    h, m = int(sec // 3600), int((sec % 3600) // 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


class MobileFulfiller:
    def __init__(self, storage: Storage, market: Marketplace, notifier: Notifier):
        self.s = storage
        self.market = market
        self.note = notifier

    # ---------- заказ ----------
    async def handle_order(self, ev: OrderEvent) -> bool:
        lot = self.s.mobile_lot_get(ev.marketplace, ev.lot_id)
        if not lot or not lot.get("enabled"):
            return False

        # Чёрный список: скамеру автоматически не выдаём — зовём владельца.
        if self.s.is_blacklisted("mobile", ev.marketplace, ev.buyer_id):
            self.s.log("mobile_blacklisted", data={"deal": ev.deal_id, "buyer": ev.buyer_name})
            await self.note.notify(
                f"🚫 Покупатель {ev.buyer_name} в чёрном списке — заказ {ev.deal_id} "
                f"(пул {lot['game']}) НЕ выдан автоматически. Проверьте вручную.")
            return True

        existing = self.s.mobile_rental_by_deal(ev.marketplace, ev.deal_id)
        if existing:
            await self._redeliver(ev.chat_id, existing)
            return True

        game = lot["game"]
        hours = int(lot.get("hours") or 24)
        acc_id = self.s.mobile_claim_free(game)
        if acc_id is None:
            self.s.warranty_add(ev.marketplace, ev.deal_id, ev.lot_id, game, "no_stock",
                                "notified", time.time())
            await self.note.notify(
                f"❌ PUBG Mobile: нет свободного аккаунта пула «{game}» для заказа {ev.deal_id}. "
                f"Гарантийный случай открыт.")
            await self.market.send_message(
                ev.chat_id, "Спасибо за заказ! Свободный аккаунт появится совсем скоро — "
                            "выдадим в ближайшее время, приносим извинения.")
            return True

        acc = self.s.mobile_get(acc_id)
        now = time.time()
        expires = now + hours * 3600
        rid = self.s.mobile_rental_add(ev.marketplace, ev.deal_id, ev.chat_id, acc_id, game,
                                       acc["login"], now, expires, "active")
        self.s.log("mobile_rent", account_id=acc_id,
                   data={"deal": ev.deal_id, "rental": rid, "login": acc["login"]})
        await self._deliver(ev.chat_id, acc, hours)
        await self.note.notify(
            f"✅ PUBG Mobile: выдан аккаунт {acc['login']} на {hours} ч "
            f"(заказ {ev.deal_id}, {ev.buyer_name}). Свободно ещё: {self.s.mobile_free_count(game)}.")
        return True

    async def _deliver(self, chat_id: str, acc: dict, hours: int) -> None:
        lt = LOGIN_LABEL.get(acc["login_type"], acc["login_type"])
        text = (
            f"📱 PUBG Mobile — аренда на {hours} ч\n\n"
            f"Вход через: {lt}\n"
            f"👤 Логин: {acc['login']}\n"
            f"🔑 Пароль: {acc['password']}\n\n"
            f"Если при входе запросят код подтверждения — напишите в чат !код, пришлём его.\n"
            f"!время — сколько осталось.\n\n"
            f"⚠️ Не меняйте пароль, почту и привязки аккаунта. По окончании срок доступ закроется."
        )
        await self.market.send_message(chat_id, text)

    async def _redeliver(self, chat_id: str, row: dict) -> None:
        try:
            acc = self.s.mobile_get(row["account_id"])
        except KeyError:
            return
        lt = LOGIN_LABEL.get(acc["login_type"], acc["login_type"])
        await self.market.send_message(
            chat_id, f"📱 Ваш доступ (вход через {lt}):\n👤 {acc['login']}\n🔑 {acc['password']}\n\n"
                     f"!код — код входа с почты, !время — остаток.")

    # ---------- команды ----------
    async def handle_command(self, msg: ChatMessage) -> bool:
        row = self.s.mobile_rental_by_chat(msg.marketplace, msg.chat_id)
        if not row:
            return False
        cmd = (msg.text or "").strip().lower()
        if cmd in CMD_TIME:
            await self.market.send_message(msg.chat_id, f"⏳ До конца аренды: {_fmt_left(row['expires'])}.")
            return True
        if cmd in CMD_CODE:
            try:
                acc = self.s.mobile_get(row["account_id"])
            except KeyError:
                return True
            if not acc.get("email") or not acc.get("email_password"):
                await self.market.send_message(
                    msg.chat_id, "Код входа по этому аккаунту не настроен — напишите продавцу.")
                self.s.log("mobile_code_unconfigured", account_id=row["account_id"])
                return True
            code, info = await asyncio.to_thread(
                mobile_otp.read_otp, acc["email"], acc["email_password"], acc.get("imap_host") or None)
            if code:
                await self.market.send_message(msg.chat_id, f"🔑 Код входа: {code}\n({info})")
            else:
                await self.market.send_message(
                    msg.chat_id, "Код пока не пришёл на почту. Запросите код в игре и напишите !код снова.")
                await self.note.notify(
                    f"⚠️ PUBG !код: не нашёл код для аренды {row.get('deal_id','')} ({acc['login']}): {info}")
            return True
        return False

    # ---------- окончание ----------
    def _autokick_enabled(self) -> bool:
        """Авто-кик по окончании аренды. По умолчанию ВЫКЛ (эксперим., зависит от провайдера)."""
        return self.s.get_setting("mobile_autokick") == "1"

    async def _autokick(self, row: dict) -> str:
        """Попытаться выкинуть арендатора. Возвращает хвост для уведомления владельцу."""
        from . import mobile_kick
        try:
            acc = self.s.mobile_get(row["account_id"])
        except KeyError:
            return ""
        try:
            d = await mobile_kick.kick(acc)
        except Exception as e:  # noqa: BLE001
            self.s.log("mobile_autokick_fail", account_id=row["account_id"], error=str(e)[:160])
            return f" 🚨 Авто-кик упал: {str(e)[:100]}. Смените доступ вручную."
        if d.get("ok"):
            self.s.log("mobile_autokick", account_id=row["account_id"])
            return " 🚪 Авто-кик выполнен."
        self.s.log("mobile_autokick_skip", account_id=row["account_id"],
                   data={"msg": d.get("message", "")})
        return f" ⚠️ Авто-кик: {d.get('message', 'не выполнен')}"

    async def expiry_tick(self) -> None:
        now = time.time()
        autokick = self._autokick_enabled()
        for row in self.s.mobile_rentals_expired(now):
            tail = await self._autokick(row) if autokick else \
                " Рекомендую сменить доступ перед следующей выдачей."
            self.s.mobile_set_state(row["account_id"], "free")
            self.s.mobile_rental_update_status(row["id"], "expired")
            self.s.log("mobile_expired", account_id=row["account_id"],
                       data={"rental": row["id"], "login": row.get("login", "")})
            await self.note.notify(
                f"⌛ PUBG Mobile: аренда {row.get('login','')} истекла (заказ {row.get('deal_id','')}). "
                f"Аккаунт снова свободен.{tail}")
            await review_request.ask_after_completion(
                self.s, self.market, row.get("chat_id"), order_id=row.get("deal_id"))
