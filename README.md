# rentbot — каркас продукта «SteamRent для Playerok»

Self-hosted бот автоматической аренды Steam-аккаунтов на Playerok. Ставится на VPS/ПК
продавца; секреты (maFile, пароли) не покидают его машину.

Это **каркас MVP**, не готовый продукт: движок аренды и хранилище работают и покрыты
тестами; адаптер Playerok и смена пароля Steam — обозначены как каркас с планом реализации
(см. TODO в коде), потому что их нельзя писать «вслепую» — нужны живой кабинет продавца и
прототип на реальных Steam-аккаунтах (неделя 1).

## Что уже работает (и протестировано)
- `rentbot/steam/guard.py` — коды Steam Guard из `shared_secret` (чистый TOTP).
- `rentbot/engine.py` — движок: выдача, очередь при пустом пуле, таймер, предупреждение,
  продление повторной покупкой, завершение, ротация с 3 ретраями и карантином при сбое,
  отдых аккаунта, синхронизация публикации лотов, лимит кодов, идемпотентность по `deal_id`.
- `rentbot/storage.py` — SQLite (stdlib), секреты шифруются `Vault` (scrypt+Fernet).
- `tests/test_engine.py` — 7 сценариев, все зелёные.

## Что каркас (TODO при интеграции)
- `rentbot/adapters/playerok.py` — HTTP (curl_cffi, Cloudflare), GraphQL/WS, опрос.
- `rentbot/steam/backend.py` — смена пароля + деавторизация (путь A Python / путь B Node-сайдкар).
- Telegram-панель владельца (aiogram), Docker, сервер лицензий.

## Запуск тестов
```
python3 tests/test_engine.py        # без зависимостей, кроме cryptography
# или: pytest -q
```

## Зависимости
См. `requirements.txt`. Ядро и тесты требуют только `cryptography` (стандартный sqlite/asyncio).
Боевые адаптеры добавляют `curl_cffi`, `websockets`, `aiogram`.

## Структура
```
rentbot/
  domain.py      модели: Account, LotBinding, Rental, состояния
  ports.py       интерфейсы: Marketplace, SteamBackend, Notifier
  vault.py       шифрование секретов (scrypt + Fernet)
  storage.py     SQLite-хранилище (claim_free_account атомарно, идемпотентность)
  engine.py      движок аренды — вся бизнес-логика, без сети
  steam/guard.py    коды Steam Guard (работает)
  steam/backend.py  смена пароля (каркас: Python / Node-сайдкар)
  adapters/playerok.py  адаптер площадки (каркас + план)
  adapters/fakes.py     фейки для тестов/офлайн-прогона
tests/test_engine.py
```
