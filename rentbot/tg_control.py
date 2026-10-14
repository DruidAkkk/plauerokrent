"""Телеграм-бот управления/мониторинга воркера (read-only).

Нужен прежде всего для headless-воркера на VPS, где нет GUI: оператор из Telegram смотрит статус,
продажи за сегодня, активные аренды и баланс KoSell. Отвечает ТОЛЬКО авторизованному чату
(тот же chat_id, что для уведомлений). Ничего не меняет — только читает.

Транспорт инъектируется (для тестов — фейк), в проде ходит на api.telegram.org через curl_cffi.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Awaitable, Callable

from .storage import Storage

TELEGRAM_API = "https://api.telegram.org"


def _since_today() -> float:
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _uptime(sec: float) -> str:
    sec = int(max(0, sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}д {h}ч {m}м"
    return f"{h}ч {m}м" if h else f"{m}м"


class TelegramControl:
    def __init__(self, storage: Storage, token: str, chat_id: str,
                 transport: Callable[[str, dict], Awaitable[dict]] | None = None,
                 started_at: float | None = None):
        self.s = storage
        self.token = token
        self.chat_id = str(chat_id)
        self._transport = transport or self._http
        self.started_at = started_at or time.time()
        self._offset = 0
        self._running = False

    async def _http(self, method: str, payload: dict) -> dict:
        from curl_cffi import requests  # ленивый импорт
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        async with requests.AsyncSession() as s:
            r = await s.post(url, json=payload, timeout=40)
            return r.json()

    async def _send(self, text: str) -> None:
        await self._transport("sendMessage", {"chat_id": self.chat_id, "text": text,
                                              "disable_web_page_preview": True})

    # ---------- команды ----------
    async def handle(self, text: str) -> str | None:
        parts = (text or "").strip().split()
        if not parts:
            return None
        cmd = parts[0].lower().lstrip("/").split("@")[0]  # /status@bot → status
        if cmd in ("start", "help"):
            return self._help()
        if cmd == "status":
            return self._status()
        if cmd == "stats":
            return self._stats()
        if cmd == "active":
            return self._active()
        if cmd == "balance":
            return await self._balance()
        return None  # неизвестная команда — молчим (не мешаем другим ботам/людям)

    @staticmethod
    def _help() -> str:
        return ("🤖 Команды управления:\n"
                "/status — состояние воркера и плагинов\n"
                "/stats — продажи за сегодня\n"
                "/active — активные аренды\n"
                "/balance — баланс KoSell")

    def _status(self) -> str:
        kosell = "вкл" if self.s.get_setting("kosell_key") else "выкл"
        pubg_free = self.s.mobile_free_count("pubg_mobile")
        ko_active = sum(1 for r in self.s.kosell_rental_list() if r.get("status") == "active")
        mob_active = sum(1 for r in self.s.mobile_rental_list() if r.get("status") == "active")
        return (f"✅ Воркер работает\n"
                f"⏱ Аптайм: {_uptime(time.time() - self.started_at)}\n"
                f"🛒 KoSell: {kosell}\n"
                f"📱 PUBG свободно: {pubg_free}\n"
                f"🔑 Активных аренд: KoSell {ko_active}, PUBG {mob_active}")

    def _stats(self) -> str:
        since = _since_today()
        native = self.s.stats(since)
        ko = sum(1 for r in self.s.kosell_rental_list(limit=500) if (r.get("created") or 0) >= since)
        mob = sum(1 for r in self.s.mobile_rental_list(limit=500) if (r.get("started") or 0) >= since)
        total = native.get("rentals", 0) + ko + mob
        return (f"📊 Сегодня:\n"
                f"Всего выдач: {total}\n"
                f"  • Steam-аренда: {native.get('rentals', 0)} ({native.get('revenue', 0):.0f}₽)\n"
                f"  • KoSell: {ko}\n"
                f"  • PUBG Mobile: {mob}")

    def _active(self) -> str:
        ko = [r for r in self.s.kosell_rental_list(limit=200) if r.get("status") == "active"]
        mob = [r for r in self.s.mobile_rental_list(limit=200) if r.get("status") == "active"]
        lines = [f"🔑 Активные аренды: {len(ko) + len(mob)}"]
        for r in ko[:8]:
            lines.append(f"  • KoSell: {r.get('product_name', '')} (заказ {r.get('deal_id', '')})")
        for r in mob[:8]:
            lines.append(f"  • PUBG: {r.get('login', '')} (заказ {r.get('deal_id', '')})")
        if len(ko) + len(mob) == 0:
            lines.append("  нет")
        return "\n".join(lines)

    async def _balance(self) -> str:
        key = self.s.get_setting("kosell_key")
        if not key:
            return "KoSell не подключён (нет API-ключа)."
        proxy = self.s.get_setting("kosell_proxy")

        def fetch():
            from .adapters.kosell import KoSellClient, KoSellError
            try:
                return KoSellClient(key, proxy).balance()
            except KoSellError as e:
                return {"_err": e.ru}

        b = await asyncio.to_thread(fetch)
        if b.get("_err"):
            return f"KoSell: {b['_err']}"
        return (f"💰 KoSell баланс: {b.get('balance_rub', 0):.2f}₽ / "
                f"${b.get('balance_usd', 0):.2f} ({b.get('username', '')})")

    # ---------- приём апдейтов ----------
    async def _on_update(self, u: dict) -> None:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = str((msg.get("chat") or {}).get("id") or "")
        if chat != self.chat_id:   # только авторизованный чат
            return
        reply = await self.handle(msg.get("text") or "")
        if reply:
            await self._send(reply)

    async def run(self) -> None:
        self._running = True
        # пропустить накопившиеся старые апдейты
        try:
            init = await self._transport("getUpdates", {"timeout": 0, "offset": -1})
            res = init.get("result") or []
            if res:
                self._offset = res[-1]["update_id"] + 1
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._send("🤖 Контроль-бот на связи. /help — команды.")
        except Exception:  # noqa: BLE001
            pass
        while self._running:
            try:
                upd = await self._transport("getUpdates", {"timeout": 30, "offset": self._offset})
                for u in upd.get("result") or []:
                    self._offset = u["update_id"] + 1
                    await self._on_update(u)
            except Exception as e:  # noqa: BLE001
                self.s.log("tg_control_error", data={"err": str(e)[:200]})
                await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
