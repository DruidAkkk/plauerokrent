"""Движок аренды — сердце продукта. Чистая логика поверх портов, тестируется без сети.

Жизненный цикл:
  оплата → выдать свободный аккаунт (или в очередь) → таймер →
  предупреждение за N мин → продление повторной покупкой → конец →
  ротация (смена пароля + деавторизация) → отдых → снова в пул →
  если из очереди кто-то ждёт — выдать ему.

Идемпотентность: один deal_id обрабатывается ровно один раз (UNIQUE в БД).
"""
from __future__ import annotations

import time

from .domain import (Account, AccountState, LotBinding, Rental, RentalState,
                     RotationMode)
from .ports import Marketplace, Notifier, OrderEvent, SteamBackend
from .storage import Storage
from .vault import mask

WARN_BEFORE_SEC = 15 * 60
REST_AFTER_RENT_SEC = 3 * 60
ROTATE_RETRIES = 3
CODE_LIMIT_PER_RENTAL = 20

TPL_ISSUE = ("🎮 Аренда началась!\nЛогин: {login}\nПароль: {password}\n"
             "Срок: {hours} ч (до {until}).\nКод Steam Guard — напишите в этот чат: !код")
TPL_QUEUED = ("⏳ Оплата получена, но сейчас все аккаунты заняты. Вы в очереди, "
              "как только освободится — выдам автоматически. Обычно это быстро.")
TPL_WARN = "⏰ До конца аренды ~15 минут. Хотите продлить — просто купите лот ещё раз."
TPL_END = "🔚 Время аренды вышло. Спасибо! Доступ к аккаунту закрыт."
TPL_EXTEND = "✅ Аренда продлена на {hours} ч, теперь до {until}."
TPL_CODE = "🔐 Код Steam Guard: {code} (действует ~{left} с)"
TPL_CODE_NONE = "Код можно получить только во время активной аренды."
TPL_CODE_LIMIT = "Лимит кодов на эту аренду исчерпан. Напишите продавцу."


def _fmt(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


class RentalEngine:
    def __init__(self, storage: Storage, market: Marketplace, steam: SteamBackend,
                 notifier: Notifier, *, clock=time.time):
        self.s = storage
        self.mk = market
        self.steam = steam
        self.note = notifier
        self.now = clock

    # ---------- входящая оплата ----------
    async def on_order(self, ev: OrderEvent) -> None:
        lot = self.s.get_lot(ev.marketplace, ev.lot_id)
        if lot is None:
            return  # лот не привязан к пулу — не наш, игнор (режим «только аренда»)

        # Продление: у покупателя уже идёт аренда по этому пулу → добавляем время.
        active = self.s.active_rental_for_buyer(ev.marketplace, ev.buyer_id, lot.game)
        if active is not None:
            # помечаем сделку обработанной, чтобы опрос не продлил дважды
            marker = Rental(None, ev.marketplace, ev.deal_id, ev.lot_id, ev.buyer_id,
                            ev.buyer_name, ev.chat_id, active.account_id,
                            RentalState.CANCELLED, self.now(), self.now(), ev.price)
            if self.s.insert_rental(marker) is None:
                return  # дубликат
            active.ends_at += lot.hours * 3600
            active.warned = False
            self.s.update_rental(active)
            self.s.log("extend", active.id, active.account_id, hours=lot.hours)
            await self.mk.send_message(ev.chat_id, TPL_EXTEND.format(
                hours=lot.hours, until=_fmt(active.ends_at)))
            return

        # Чёрный список: скамеру аккаунт НЕ выдаём. Сделку помечаем CANCELLED (идемпотентно и
        # вне очереди — _drain_queues её не подхватит), зовём владельца. Покупателю не пишем,
        # чтобы не подсказывать скамеру; решение по сделке — за владельцем.
        if self.s.is_blacklisted("steam", ev.marketplace, ev.buyer_id):
            marker = Rental(None, ev.marketplace, ev.deal_id, ev.lot_id, ev.buyer_id,
                            ev.buyer_name, ev.chat_id, None, RentalState.CANCELLED,
                            self.now(), self.now(), ev.price)
            if self.s.insert_rental(marker) is None:
                return  # сделку уже обработали
            self.s.log("blacklist_block", data={"deal": ev.deal_id, "buyer": ev.buyer_id})
            await self.note.notify(
                f"🚫 Покупатель {ev.buyer_name} в ЧС — заказ {ev.deal_id} ({lot.game}) "
                f"НЕ выдан автоматически, проверьте вручную.")
            return

        rent = Rental(None, ev.marketplace, ev.deal_id, ev.lot_id, ev.buyer_id, ev.buyer_name,
                      ev.chat_id, None, RentalState.QUEUED, self.now(), 0.0, ev.price)
        if self.s.insert_rental(rent) is None:
            return  # идемпотентность: сделку уже обработали

        await self._try_assign(rent, lot)

    async def _try_assign(self, rent: Rental, lot: LotBinding) -> bool:
        acc = self.s.claim_free_account(lot.game, self.now())
        if acc is None:
            self.s.log("queued", rent.id, data={"game": lot.game})
            await self.mk.send_message(rent.chat_id, TPL_QUEUED)
            await self.note.notify(f"⏳ Нет свободных «{lot.game}», заказ {rent.deal_id} в очереди.")
            return False
        rent.account_id = acc.id
        rent.state = RentalState.ACTIVE
        rent.started_at = self.now()
        rent.ends_at = self.now() + lot.hours * 3600
        self.s.update_rental(rent)
        self.s.log("issue", rent.id, acc.id)
        await self.mk.send_message(rent.chat_id, TPL_ISSUE.format(
            login=acc.login, password=acc.password, hours=lot.hours, until=_fmt(rent.ends_at)))
        await self.note.notify(f"▶️ Выдан {acc.login} ({lot.game}) покупателю {rent.buyer_name}.")
        return True

    # ---------- команды из чата ----------
    async def on_chat(self, marketplace: str, chat_id: str, buyer_id: str, text: str) -> None:
        cmd = text.strip().lower()
        if cmd not in ("!код", "!code", "!время", "!time"):
            return
        rent = self.s.active_rental_for_chat(marketplace, chat_id)
        if rent is None or rent.account_id is None:
            await self.mk.send_message(chat_id, TPL_CODE_NONE)
            return
        if cmd in ("!время", "!time"):
            left = max(0, int(rent.ends_at - self.now()))
            await self.mk.send_message(chat_id, f"⏳ Осталось ~{left // 60} мин.")
            return
        # !код
        if rent.codes_issued >= CODE_LIMIT_PER_RENTAL:
            await self.mk.send_message(chat_id, TPL_CODE_LIMIT)
            return
        acc = self.s.get_account(rent.account_id)
        if not acc.shared_secret:
            await self.mk.send_message(chat_id, "У этого аккаунта нет Steam Guard через бота.")
            return
        code = self.steam.guard_code(acc.shared_secret)
        rent.codes_issued += 1
        self.s.update_rental(rent)
        self.s.log("code", rent.id, acc.id)
        await self.mk.send_message(chat_id, TPL_CODE.format(code=code, left=25))

    # ---------- периодический тик (планировщик) ----------
    async def tick(self) -> None:
        now = self.now()
        # 1) предупреждения
        for rent in self.s.rentals_by_state(RentalState.ACTIVE):
            if not rent.warned and 0 < rent.ends_at - now <= WARN_BEFORE_SEC:
                rent.warned = True
                self.s.update_rental(rent)
                await self.mk.send_message(rent.chat_id, TPL_WARN)
        # 2) завершения
        for rent in self.s.rentals_by_state(RentalState.ACTIVE):
            if rent.ends_at <= now:
                await self._end_rental(rent)
        # 3) освободившиеся аккаунты → раздать очереди
        await self._drain_queues()

    async def _end_rental(self, rent: Rental) -> None:
        rent.state = RentalState.ENDED
        self.s.update_rental(rent)
        try:
            await self.mk.send_message(rent.chat_id, TPL_END)
        except Exception:
            pass
        self.s.log("end", rent.id, rent.account_id)
        if rent.account_id is not None:
            await self._secure_account(rent.account_id)

    def _autokick_enabled(self) -> bool:
        """Авто-кик (выход со всех устройств) по окончании аренды. По умолчанию включён."""
        return self.s.get_setting("steam_autokick") != "0"

    async def _secure_account(self, account_id: int) -> None:
        """Закрыть доступ арендатора после аренды: авто-кик (без смены пароля) либо ротация.
        Если авто-кик включён и прошёл — аккаунт сразу идёт отдыхать и снова в пул. Если кик
        не удался — подстраховываемся старой схемой (_rotate: ротация/ручная/карантин)."""
        if self._autokick_enabled():
            if await self._autokick(account_id):
                self.s.set_account_state(account_id, AccountState.RESTING,
                                         rest_until=self.now() + REST_AFTER_RENT_SEC)
                return
            # кик не удался — не оставляем аккаунт открытым, уходим в старую схему
        await self._rotate(account_id)

    async def _autokick(self, account_id: int) -> bool:
        """«Выйти со всех устройств» для аккаунта через SteamBackend. True — успех."""
        acc = self.s.get_account(account_id)
        if not acc.shared_secret:
            await self.note.notify(
                f"⚠️ Авто-кик {acc.login}: нет maFile/Guard у бота — пропускаю, закрой доступ сам.")
            return False
        try:
            d = await self.steam.deauth(login=acc.login, password=acc.password,
                                        shared_secret=acc.shared_secret, proxy=acc.proxy)
        except Exception as e:  # noqa: BLE001
            self.s.log("autokick_fail", account_id=account_id, error=str(e)[:200])
            await self.note.notify(f"🚨 Авто-кик не удался: {acc.login} ({str(e)[:120]}).")
            return False
        if d and d.get("ok"):
            n = int(d.get("total") or 0)
            self.s.log("autokick", account_id=account_id, data={"n": n})
            await self.note.notify(
                f"🚪 Авто-кик: {acc.login} — вышли со всех устройств"
                + (f" (сессий: {n})" if n else "") + ".")
            return True
        msg = (d or {}).get("message", "неизвестная ошибка")
        self.s.log("autokick_fail", account_id=account_id, error=str(msg)[:200])
        await self.note.notify(f"🚨 Авто-кик не удался: {acc.login} — {msg}.")
        return False

    async def _rotate(self, account_id: int) -> None:
        acc = self.s.get_account(account_id)
        lot_game = acc.game
        rotation = self._rotation_mode(lot_game)
        if rotation is not RotationMode.AUTO or not acc.shared_secret:
            # semi/manual: ставим задачу человеку, аккаунт ждёт подтверждения
            self.s.set_account_state(account_id, AccountState.ROTATING)
            await self.note.notify(
                f"🔑 Смени пароль вручную: {acc.login} ({lot_game}). После — /done {account_id}.")
            return
        self.s.set_account_state(account_id, AccountState.ROTATING)
        for attempt in range(1, ROTATE_RETRIES + 1):
            try:
                new_pwd = await self.steam.rotate_credentials(
                    login=acc.login, password=acc.password, shared_secret=acc.shared_secret,
                    identity_secret=acc.identity_secret or "", proxy=acc.proxy)
                self.s.set_password(account_id, new_pwd)
                self.s.set_account_state(account_id, AccountState.RESTING,
                                         rest_until=self.now() + REST_AFTER_RENT_SEC)
                self.s.log("rotated", account_id=account_id)
                return
            except Exception as e:  # noqa: BLE001
                self.s.log("rotate_fail", account_id=account_id,
                           attempt=attempt, error=str(e)[:200])
        # не вышло — карантин + алерт
        self.s.set_account_state(account_id, AccountState.QUARANTINE,
                                 note="ротация не удалась")
        await self.note.notify(
            f"🚨 Карантин: не смог сменить пароль {acc.login} (secret {mask(acc.shared_secret)}). "
            f"Проверь вручную, следующему арендатору аккаунт не выдаётся.")

    async def manual_rotated(self, account_id: int) -> None:
        """Владелец подтвердил ручную смену пароля (semi/manual)."""
        self.s.set_account_state(account_id, AccountState.RESTING,
                                 rest_until=self.now() + REST_AFTER_RENT_SEC)
        self.s.log("rotated_manual", account_id=account_id)
        await self._drain_queues()

    async def _drain_queues(self) -> None:
        now = self.now()
        # вернуть отдохнувших в пул
        for acc in self.s.resting_due(now):
            self.s.set_account_state(acc.id, AccountState.FREE)
        # для каждого пула с очередью — попытаться выдать
        games = {lot.game for lot in self._all_bound_lots()}
        for game in games:
            queue = self.s.queued_for_game(game)
            for rent in queue:
                if self.s.free_count(game, now) <= 0:
                    break
                lot = self.s.get_lot(rent.marketplace, rent.lot_id)
                if lot is None:
                    continue
                await self._try_assign(rent, lot)

    # ---------- синхронизация публикации лотов ----------
    async def sync_lot_visibility(self) -> None:
        """Снять лот с публикации, если в пуле нет свободных; вернуть — если есть."""
        now = self.now()
        for lot in self._all_bound_lots():
            active = self.s.free_count(lot.game, now) > 0
            try:
                await self.mk.set_lot_active(lot.lot_id, active)
            except Exception:
                pass

    # ---------- helpers ----------
    def _all_bound_lots(self) -> list[LotBinding]:
        games = {a.game for a in self.s.list_accounts()}
        out: list[LotBinding] = []
        for g in games:
            out.extend(self.s.lots_for_game(g))
        return out

    def _rotation_mode(self, game: str) -> RotationMode:
        for lot in self.s.lots_for_game(game):
            return lot.rotation
        return RotationMode.AUTO
