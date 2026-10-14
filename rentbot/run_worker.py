"""Headless-воркер для VPS: без GUI, ключ шифрования из env (PBKDF2→Fernet).

Запуск на сервере клиента (секрет и соль задаёт САМ клиент — разработчик их не знает):

    export ENCRYPTION_SECRET='длинная-случайная-строка'
    export ENCRYPTION_SALT='другая-случайная-строка'
    export RENTBOT_DB='/var/lib/rentbot/rentbot.db'
    python -m rentbot.run_worker

Зашифрованная БД лежит на этой же VPS. Токен Playerok и прочие настройки должны быть уже внесены
(через панель, запущенную с ТЕМ ЖЕ ENCRYPTION_SECRET/SALT и той же БД). Секреты в логи не пишутся.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from .notify import NullNotifier, TelegramNotifier
from .steam.backend import PySteamBackend
from .storage import Storage
from .vault import ENV_SALT, ENV_SECRET, Vault, env_configured
from .worker import build_worker

log = logging.getLogger("rentbot.worker")


def main() -> int:
    logging.basicConfig(level=os.getenv("RENTBOT_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")

    if not env_configured():
        log.error("Не заданы переменные окружения %s и %s — нечем расшифровать БД.",
                  ENV_SECRET, ENV_SALT)
        return 2

    db_path = os.getenv("RENTBOT_DB", "rentbot.db")
    try:
        vault = Vault.from_env()
        storage = Storage(db_path, vault)
    except Exception as e:  # noqa: BLE001
        log.error("Не открыть хранилище: %s: %s", type(e).__name__, e)
        return 3

    token = storage.get_setting("pl_token")
    if not token:
        log.error("В БД нет токена Playerok (pl_token). Настройте через панель на этой же БД "
                  "и с тем же ENCRYPTION_SECRET/SALT.")
        return 4
    proxy = storage.get_setting("pl_proxy")
    tg_token = storage.get_setting("tg_token")
    tg_chat = storage.get_setting("tg_chat")
    notifier = (TelegramNotifier(tg_token, tg_chat) if tg_token and tg_chat else NullNotifier())

    worker = build_worker(storage, token=token, proxy=proxy,
                          steam=PySteamBackend(), notifier=notifier)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown() -> None:
        log.info("Получен сигнал остановки — завершаю…")
        worker.stop()
        for t in asyncio.all_tasks(loop):
            t.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: _shutdown())

    log.info("Воркер запущен (БД %s). Плагины подключатся по настройкам из БД.", db_path)
    try:
        loop.run_until_complete(worker.run())
    except asyncio.CancelledError:
        pass
    except Exception as e:  # noqa: BLE001
        log.error("Воркер аварийно остановлен: %s: %s", type(e).__name__, e)
        return 1
    finally:
        try:
            storage.db.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("Воркер остановлен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
