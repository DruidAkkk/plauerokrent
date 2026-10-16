"""Автопоставка KoSell для live-воркера.

Связывает заказ Playerok с арендой на KoSell: когда покупают привязанный лот, берём аренду
у KoSell, отдаём покупателю логин+пароль в чат, обслуживаем !код/!время и по окончании срока
помечаем аренду завершённой. У нас на стороне НЕТ Steam-логина — всё делает KoSell.

KoSell API синхронный (curl_cffi), воркер асинхронный → сетевые вызовы уводим в поток через
asyncio.to_thread, чтобы не блокировать event-loop.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from .adapters.kosell import KoSellClient, KoSellError
from .ports import ChatMessage, Marketplace, Notifier, OrderEvent
from .storage import Storage

CMD_CODE = ("!код", "!code")
CMD_TIME = ("!время", "!time")
CMD_REPLACE = ("!замена", "!replace", "!проблема", "!problem")


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_left(expires_at: str) -> str:
    dt = _parse_iso(expires_at)
    if not dt:
        return "неизвестно"
    sec = (dt - datetime.now(timezone.utc)).total_seconds()
    if sec <= 0:
        return "истекла"
    h, m = int(sec // 3600), int((sec % 3600) // 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


class KoSellFulfiller:
    def __init__(self, storage: Storage, market: Marketplace, notifier: Notifier, *,
                 key: str, proxy: str | None = None, currency: str = "RUB", retries: int = 2):
        self.s = storage
        self.market = market
        self.note = notifier
        self.client = KoSellClient(key, proxy)
        self.currency = currency if currency in ("RUB", "USD") else "RUB"
        self.retries = max(0, int(retries))   # автоповтор аренды при no_stock (гарантия)

    # ---------- заказ ----------
    async def handle_order(self, ev: OrderEvent) -> bool:
        """True, если заказ относится к KoSell (лот привязан) и мы его обработали."""
        m = self.s.kosell_map_get(ev.marketplace, ev.lot_id)
        if not m or not m.get("enabled"):
            return False  # не наш лот — пусть идёт по другим путям воркера

        # Чёрный список: скамеру автоматически не выдаём — зовём владельца.
        if self.s.is_blacklisted("kosell", ev.marketplace, ev.buyer_id):
            self.s.log("kosell_blacklisted", data={"deal": ev.deal_id, "buyer": ev.buyer_name})
            await self.note.notify(
                f"🚫 Покупатель {ev.buyer_name} в чёрном списке — заказ {ev.deal_id} "
                f"({m.get('product_name', '')}) НЕ выдан автоматически. Проверьте вручную.")
            return True

        # идемпотентность: если по этой сделке аренда уже бралась — не берём повторно,
        # но доступ перешлём (покупатель мог потерять сообщение).
        existing = self.s.kosell_rental_by_deal(ev.marketplace, ev.deal_id)
        if existing:
            await self._redeliver(ev.chat_id, existing)
            return True

        hours = int(m.get("hours") or 24)
        product_id = int(m["product_id"])
        pname = m.get("product_name", "")

        # Гарантия: при no_stock повторяем аренду (сток у KoSell пополняется). idempotency_key
        # один и тот же (id заказа) — повторы не создают двойную аренду/списание.
        rr, last_err = None, None
        attempts = self.retries + 1
        for i in range(attempts):
            try:
                rr = await asyncio.to_thread(self.client.rent, product_id, hours,
                                             self.currency, ev.deal_id)
                break
            except KoSellError as e:
                last_err = e
                if e.code == "no_stock" and i < attempts - 1:
                    self.s.log("kosell_retry", data={"deal": ev.deal_id, "attempt": i + 1})
                    await asyncio.sleep(3 + i * 3)
                    continue
                break

        if rr is None:
            reason = "no_stock" if (last_err and last_err.code == "no_stock") else "rent_error"
            self.s.warranty_add(ev.marketplace, ev.deal_id, ev.lot_id, pname, reason,
                                "notified", time.time())
            self.s.log("kosell_order_error", data={"deal": ev.deal_id, "lot": ev.lot_id,
                       "err": last_err.ru if last_err else "unknown"})
            await self.note.notify(
                f"❌ KoSell: не выдал аренду по заказу {ev.deal_id} (лот {ev.lot_id}): "
                f"{last_err.ru if last_err else 'ошибка'}. Гарантийный случай открыт.")
            if last_err and last_err.code == "no_stock":
                await self.market.send_message(
                    ev.chat_id, "Спасибо за заказ! Сейчас временно нет свободного аккаунта — "
                                "выдадим в ближайшее время. Приносим извинения за задержку.")
            return True  # лот наш, но выдать не смогли — дальше по воркеру не пускаем

        uid = rr["rental_uid"]
        self.s.kosell_rental_add(uid, ev.marketplace, ev.deal_id, ev.chat_id, product_id,
                                 rr.get("product_name", m.get("product_name", "")),
                                 rr.get("steam_login", ""), hours, rr.get("started_at", ""),
                                 rr.get("expires_at", ""), rr.get("status", "active"), time.time())
        self.s.log("kosell_rent", account_id=None,
                   data={"deal": ev.deal_id, "uid": uid, "product": rr.get("product_name", "")})

        # доступ покупателю
        try:
            cred = await asyncio.to_thread(self.client.credentials, uid)
        except KoSellError as e:
            await self.note.notify(f"⚠️ KoSell {ev.deal_id}: аренда взята ({uid}), но креды не "
                                   f"получить: {e.ru}")
            cred = None
        if cred:
            await self._deliver(ev.chat_id, rr, cred, hours)
        await self.note.notify(
            f"✅ KoSell: выдана аренда {rr.get('product_name','')} на {hours} ч "
            f"(заказ {ev.deal_id}, {ev.buyer_name}).")
        return True

    async def _deliver(self, chat_id: str, rr: dict, cred: dict, hours: int) -> None:
        text = (
            f"🎮 {rr.get('product_name','Steam')} — аренда на {hours} ч\n\n"
            f"👤 Логин: {cred.get('steam_login','')}\n"
            f"🔑 Пароль: {cred.get('steam_password','')}\n"
            f"⏳ Аренда до: {rr.get('expires_at','')} (UTC)\n\n"
            f"Команды в чате:\n"
            f"  !код — код Steam Guard для входа\n"
            f"  !время — сколько осталось\n\n"
            f"⚠️ Не меняйте пароль и почту аккаунта. По окончании срока доступ закроется автоматически."
        )
        await self.market.send_message(chat_id, text)

    async def _redeliver(self, chat_id: str, row: dict) -> None:
        try:
            cred = await asyncio.to_thread(self.client.credentials, row["rental_uid"])
        except KoSellError:
            return
        await self.market.send_message(
            chat_id,
            f"🎮 {row.get('product_name','Steam')} — ваш доступ\n\n"
            f"👤 Логин: {cred.get('steam_login','')}\n"
            f"🔑 Пароль: {cred.get('steam_password','')}\n\n"
            f"!код — код Steam Guard, !время — остаток.")

    # ---------- команды в чате ----------
    async def handle_command(self, msg: ChatMessage) -> bool:
        """Обработать !код/!время для KoSell-чата. True — если это наш чат и команда разобрана."""
        row = self.s.kosell_rental_by_chat(msg.marketplace, msg.chat_id)
        if not row:
            return False
        cmd = (msg.text or "").strip().lower()
        if cmd in CMD_CODE:
            try:
                r = await asyncio.to_thread(self.client.code, row["rental_uid"])
            except KoSellError as e:
                await self.market.send_message(msg.chat_id, f"Не удалось получить код: {e.ru}")
                return True
            if r.get("success") and r.get("code"):
                await self.market.send_message(
                    msg.chat_id, f"🔑 Код Steam Guard: {r['code']}\n"
                                 f"Действует ещё ~{r.get('expires_in','?')} c.")
            else:
                await self.market.send_message(
                    msg.chat_id, f"Код пока недоступен: {r.get('error') or 'попробуйте ещё раз'}")
            return True
        if cmd in CMD_TIME:
            await self.market.send_message(
                msg.chat_id, f"⏳ До конца аренды: {_fmt_left(row.get('expires_at',''))}.")
            return True
        if cmd in CMD_REPLACE:
            # Гарантия: перевыдаём доступ + свежий код, фиксируем случай, зовём владельца.
            await self._redeliver(msg.chat_id, row)
            try:
                r = await asyncio.to_thread(self.client.code, row["rental_uid"])
                if r.get("code"):
                    await self.market.send_message(msg.chat_id, f"🔑 Новый код Steam Guard: {r['code']}")
            except KoSellError:
                pass
            self.s.warranty_add(msg.marketplace, row.get("deal_id"), None,
                                row.get("product_name", ""), "buyer_replace", "resent_creds", time.time())
            await self.note.notify(
                f"🛡️ Гарантия: покупатель просит замену (заказ {row.get('deal_id','')}, "
                f"{row.get('product_name','')}). Переслал доступ+код; проверьте случай.")
            return True
        return False

    # ---------- фоновая проверка окончания ----------
    async def expiry_tick(self) -> None:
        """Пометить истёкшие аренды и уведомить владельца. KoSell закрывает доступ сам."""
        now = datetime.now(timezone.utc)
        for row in self.s.kosell_rental_list(limit=100):
            if row.get("status") != "active":
                continue
            dt = _parse_iso(row.get("expires_at", ""))
            if dt and dt <= now:
                self.s.kosell_rental_update_status(row["rental_uid"], "expired")
                self.s.log("kosell_expired", data={"uid": row["rental_uid"],
                           "product": row.get("product_name", "")})
                await self.note.notify(
                    f"⌛ KoSell: аренда {row.get('product_name','')} истекла "
                    f"(заказ {row.get('deal_id','')}). Доступ закрыт.")
