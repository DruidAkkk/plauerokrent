# Запуск rentbot на VPS клиента (headless-воркер, ключ из env)

Данные (зашифрованная БД) и воркер живут на VPS **клиента**. Ключ шифрования выводится из двух
переменных окружения, которые задаёт **сам клиент**. Разработчик их не знает — расшифровать
чужие данные он не может.

## 1. Сгенерировать секрет и соль (на VPS, один раз)

```bash
python - <<'PY'
import secrets
print("ENCRYPTION_SECRET=" + secrets.token_urlsafe(48))
print("ENCRYPTION_SALT="   + secrets.token_urlsafe(16))
PY
```

Сохраните обе строки в надёжном месте. **Если потеряете — расшифровать БД будет нельзя.**

## 2. Внести настройки (токен Playerok, Telegram, ключ KoSell и т.д.)

Настройки шифруются ТЕМ ЖЕ ключом, что и читает воркер. Поэтому вносить их надо с теми же
`ENCRYPTION_SECRET` / `ENCRYPTION_SALT` и в ту же БД. Два способа:

- **Панель с env-ключом.** Запустите десктоп-панель с этими переменными окружения — она войдёт
  без мастер-пароля (env-режим) и будет писать в БД тем же ключом. Настройте токен Playerok,
  Telegram, плагины, аккаунты. Затем скопируйте файл БД на VPS (путь из `RENTBOT_DB`).
- Либо настраивайте прямо на VPS, если панель запускается там.

## 3. Запустить воркер

```bash
export ENCRYPTION_SECRET='...'      # из шага 1
export ENCRYPTION_SALT='...'
export RENTBOT_DB='/var/lib/rentbot/rentbot.db'
python -m rentbot.run_worker
```

Коды выхода: `2` — не заданы env-переменные; `3` — не открыть БД; `4` — в БД нет токена Playerok.

## 4. Автозапуск через systemd (24/7)

`/etc/systemd/system/rentbot.service`:

```ini
[Unit]
Description=rentbot worker
After=network-online.target

[Service]
WorkingDirectory=/opt/rentbot
Environment=ENCRYPTION_SECRET=...
Environment=ENCRYPTION_SALT=...
Environment=RENTBOT_DB=/var/lib/rentbot/rentbot.db
ExecStart=/opt/rentbot/.venv/bin/python -m rentbot.run_worker
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now rentbot
journalctl -u rentbot -f        # логи (секреты не печатаются)
```

> Храните `.service` с секретами в режиме `chmod 600`. Для Node-сайдкара Steam (если используете
> нативный вход) на VPS нужен установленный Node.js и `npm install` в папке `sidecar/`.
