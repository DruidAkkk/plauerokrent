"""Live-воркер: связывает адаптер площадки, движок аренды и базовые сервисы.

Маршрутизация событий:
  • Оплата (on_order):
      – лот привязан к пулу аренды  → RentalEngine.on_order (выдача/очередь/продление);
      – иначе есть автовыдача по лоту → AutoDelivery.deliver (текст/ключи/файл);
      – иначе лот боту не известен    → только уведомить владельца.
  • Сообщение в чате (on_chat):
      – команда !код/!время → RentalEngine.on_chat;
      – иначе → приветствие нового покупателя + автоответчик по ключевым словам;
      – в любом случае уведомить владельца о входящем сообщении.

Фоновые циклы: опрос площадки (adapter.run), тик движка (таймеры аренд), тик автоподнятия.

Антибан: все исходящие сообщения идут через HumanDelayMarket — человеческая пауза перед
отправкой. Интервалы опроса/поднятия разнесены джиттером (в адаптере и здесь).
"""
from __future__ import annotations

import asyncio
import random
import threading
import time

from .adapters.playerok import PlayerokAdapter
from .ports import ChatMessage, Marketplace, Notifier, OrderEvent, SteamBackend
from .services import AutoDelivery, AutoResponder, Bumper
from .storage import Storage


class HumanDelayMarket(Marketplace):
    """Обёртка площадки: человеческая пауза перед каждым исходящим сообщением (антибан).
    Остальные вызовы (поднятие, видимость) проксируются как есть."""
    def __init__(self, inner: Marketplace, lo: float = 1.5, hi: float = 4.0):
        self.inner = inner
        self.lo, self.hi = lo, hi
        self.name = getattr(inner, "name", "market")

    async def _pause(self) -> None:
        if self.hi > 0:
            await asyncio.sleep(random.uniform(self.lo, self.hi))

    async def send_message(self, chat_id: str, text: str) -> None:
        await self._pause()
        await self.inner.send_message(chat_id, text)

    async def send_file(self, chat_id: str, file_path: str, caption: str = "") -> None:
        await self._pause()
        await self.inner.send_file(chat_id, file_path, caption)

    async def set_lot_active(self, lot_id: str, active: bool) -> None:
        await self.inner.set_lot_active(lot_id, active)

    async def bump_item(self, lot_id: str, price: float) -> None:
        await self.inner.bump_item(lot_id, price)


class Worker:
    def __init__(self, storage: Storage, adapter, engine, delivery: AutoDelivery,
                 responder: AutoResponder, bumper: Bumper, notifier: Notifier, *,
                 kosell=None, mobile=None, control=None,
                 tick_interval: float = 20.0, bump_interval: float = 60.0):
        self.s = storage
        self.adapter = adapter
        self.engine = engine
        self.delivery = delivery
        self.responder = responder
        self.bumper = bumper
        self.note = notifier
        self.kosell = kosell                 # KoSellFulfiller | None (плагин реселла)
        self.mobile = mobile                 # MobileFulfiller | None (плагин PUBG Mobile)
        self.control = control               # TelegramControl | None (команды из Telegram)
        self.tick_interval = tick_interval
        self.bump_interval = bump_interval
        self._running = False

    # ---------- маршрутизация событий ----------
    async def on_order(self, ev: OrderEvent) -> None:
        # Плагины с приоритетом (по привязке лота): KoSell-реселл, затем PUBG Mobile
        if self.kosell is not None and await self.kosell.handle_order(ev):
            return
        if self.mobile is not None and await self.mobile.handle_order(ev):
            return
        lot = self.s.get_lot(ev.marketplace, ev.lot_id)
        if lot is not None:
            await self.engine.on_order(ev)            # аренда (сам уведомляет владельца)
            return
        res = await self.delivery.deliver(ev)          # автовыдача (сама уведомляет владельца)
        if not res.get("delivered") and res.get("reason") == "нет автовыдачи для лота":
            await self.note.notify(
                f"🛒 Новый заказ {ev.deal_id}: лот {ev.lot_id}, {ev.price}₽, "
                f"{ev.buyer_name} (лот не настроен в боте)")

    async def on_chat(self, msg: ChatMessage) -> None:
        # адаптер отдаёт ChatMessage одним объектом (контракт ports.Marketplace)
        cmd = (msg.text or "").strip().lower()
        kosell_cmds = ("!код", "!code", "!время", "!time",
                       "!замена", "!replace", "!проблема", "!problem")
        # KoSell-чат? его команды (включая гарантийную !замена) обслуживает фулфиллер
        if cmd in kosell_cmds and self.kosell is not None and await self.kosell.handle_command(msg):
            return
        if cmd in ("!код", "!code", "!время", "!time"):
            # PUBG Mobile чат? иначе — движок Steam-аренды
            if self.mobile is not None and await self.mobile.handle_command(msg):
                return
            await self.engine.on_chat(msg.marketplace, msg.chat_id, msg.buyer_id, msg.text)
            return
        await self.responder.greet(msg.marketplace, msg.chat_id)   # один раз на чат
        await self.responder.respond(msg)
        await self.note.notify(f"✉️ Сообщение (чат {msg.chat_id}): {(msg.text or '')[:120]}")

    # ---------- фоновые циклы ----------
    async def _tick_loop(self) -> None:
        while self._running:
            try:
                await self.engine.tick()
            except Exception as e:  # noqa: BLE001
                self.s.log("tick_error", data={"err": str(e)[:200]})
            await asyncio.sleep(self.tick_interval)

    async def _bump_loop(self) -> None:
        while self._running:
            try:
                await self.bumper.tick()
            except Exception as e:  # noqa: BLE001
                self.s.log("bump_loop_error", data={"err": str(e)[:200]})
            await asyncio.sleep(self.bump_interval + random.uniform(-5, 5))

    async def _plugin_expiry_loop(self) -> None:
        while self._running:
            try:
                if self.kosell is not None:
                    await self.kosell.expiry_tick()
                if self.mobile is not None:
                    await self.mobile.expiry_tick()
            except Exception as e:  # noqa: BLE001
                self.s.log("expiry_loop_error", data={"err": str(e)[:200]})
            await asyncio.sleep(self.tick_interval * 3)  # реже движка: окончание не критично к секундам

    async def run(self) -> None:
        self._running = True
        self.s.log("worker_start")
        tasks = [self.adapter.run(self.on_order, self.on_chat), self._tick_loop(), self._bump_loop()]
        if self.kosell is not None or self.mobile is not None:
            tasks.append(self._plugin_expiry_loop())
        if self.control is not None:
            tasks.append(self.control.run())
        try:
            await asyncio.gather(*tasks)
        except Exception as e:  # noqa: BLE001 — крах воркера фиксируем в журнал для диагностики
            self.s.log("worker_crash", data={"err": f"{type(e).__name__}: {str(e)[:400]}"})
            raise
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False
        if self.control is not None:
            self.control.stop()


def build_worker(storage: Storage, *, token: str, proxy: str | None,
                 steam: SteamBackend, notifier: Notifier,
                 human_delay: tuple[float, float] = (1.5, 4.0)) -> Worker:
    """Собрать воркер поверх живого адаптера Playerok и переданного хранилища."""
    from .engine import RentalEngine
    adapter = PlayerokAdapter(token, proxy=proxy)
    # сбой опроса (например, неожиданный ответ площадки) — в журнал, воркер продолжает жить
    adapter.on_poll_error = lambda e: storage.log(
        "poll_error", data={"err": f"{type(e).__name__}: {str(e)[:300]}"})
    market = HumanDelayMarket(adapter, *human_delay)
    engine = RentalEngine(storage, market, steam, notifier)
    delivery = AutoDelivery(storage, market, notifier)
    responder = AutoResponder(storage, market)
    bumper = Bumper(storage, market, notifier)

    # Плагин KoSell-реселла: включается, если задан API-ключ (Настройки → KoSell)
    kosell = None
    kkey = storage.get_setting("kosell_key")
    if kkey:
        from .kosell_service import KoSellFulfiller
        # гарантия: число автоповторов аренды при no_stock (0 = выключено)
        retries = 0 if storage.get_setting("warranty_enabled") == "0" else \
            int(storage.get_setting("warranty_autoretry") or 2)
        kosell = KoSellFulfiller(storage, market, notifier, key=kkey,
                                 proxy=storage.get_setting("kosell_proxy"),
                                 currency=storage.get_setting("kosell_currency") or "RUB",
                                 retries=retries)

    # Плагин PUBG Mobile: нужен только storage/market/notifier, включаем всегда
    # (handle_order вернёт False, если лот не привязан к мобильному пулу).
    from .mobile_service import MobileFulfiller
    mobile = MobileFulfiller(storage, market, notifier)

    # Телеграм-бот управления (read-only): если задан токен и chat_id
    control = None
    tg_token = storage.get_setting("tg_token")
    tg_chat = storage.get_setting("tg_chat")
    if tg_token and tg_chat:
        from .tg_control import TelegramControl
        control = TelegramControl(storage, tg_token, tg_chat)

    return Worker(storage, adapter, engine, delivery, responder, bumper, notifier,
                  kosell=kosell, mobile=mobile, control=control)


class ThreadedWorker:
    """Запуск воркера в фоновом потоке со своим event-loop и СВОИМ подключением к БД.

    Панель (pywebview) живёт в главном потоке; SQLite-подключение нельзя делить между
    потоками, поэтому воркер открывает отдельный Storage на тот же файл (WAL). Vault
    (ключ шифрования) переиспользуется — он уже выведен из мастер-пароля при разблокировке.
    """
    def __init__(self, db_path: str, vault, *, settings_reader=None):
        self.db_path = db_path
        self.vault = vault
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker: Worker | None = None
        self.last_error: str | None = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> dict:
        if self.is_running():
            return {"error": "воркер уже запущен"}
        # читаем настройки через временный Storage (главный поток)
        probe = Storage(self.db_path, self.vault)
        token = probe.get_setting("pl_token")
        proxy = probe.get_setting("pl_proxy")
        tg_token = probe.get_setting("tg_token")
        tg_chat = probe.get_setting("tg_chat")
        probe.db.close()
        if not token:
            return {"error": "Не задан токен Playerok (Настройки → Playerok)"}

        self.last_error = None
        ready = threading.Event()
        err: dict = {}

        def runner():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                storage = Storage(self.db_path, self.vault)  # своё подключение в этом потоке
                from .steam.backend import PySteamBackend
                from .notify import TelegramNotifier, NullNotifier
                notifier = (TelegramNotifier(tg_token, tg_chat)
                            if tg_token and tg_chat else NullNotifier())
                self._worker = build_worker(storage, token=token, proxy=proxy,
                                            steam=PySteamBackend(), notifier=notifier)
                ready.set()
                loop.run_until_complete(self._worker.run())
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
                err["e"] = self.last_error
            finally:
                ready.set()

        self._thread = threading.Thread(target=runner, name="rentbot-worker", daemon=True)
        self._thread.start()
        ready.wait(timeout=10)
        if err.get("e"):
            return {"error": err["e"]}
        return {"ok": True}

    def stop(self) -> dict:
        if not self.is_running() or self._loop is None or self._worker is None:
            self._thread = None
            return {"ok": True}
        try:
            self._worker.stop()
            # разбудить/остановить loop: отменяем все задачи
            def _cancel():
                for t in asyncio.all_tasks(self._loop):
                    t.cancel()
            self._loop.call_soon_threadsafe(_cancel)
            self._thread.join(timeout=8)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
        self._thread = None
        self._worker = None
        self._loop = None
        return {"ok": True}
