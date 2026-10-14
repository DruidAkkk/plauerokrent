# Сборка установщика rentbot (Windows)

Превращает приложение (Python + pywebview) в автономный `rentbot.exe` и установщик
`rentbot-Setup.exe` — по принципу FunPay Pulse: пользователь скачивает Setup, ставит, открывает.
Питон и зависимости пользователю не нужны.

## Что внутри
- `run.py` — точка входа для фриза (PyInstaller анализирует приложение как пакет `app`).
- `rentbot.spec` — спецификация PyInstaller: onedir, оконное приложение, вкладывает `app/web` → `web`
  и `sidecar` → `sidecar`, собирает `curl_cffi` (бинарный libcurl) и все подмодули `rentbot`/`app`.
- `make_icon.py` — генерирует `rentbot.ico` (градиент + молния).
- `installer.iss` — скрипт Inno Setup: установка в Program Files, ярлыки (Пуск + рабочий стол),
  деинсталлятор, иконка.

## Требования (один раз)
- Python-venv проекта с установленными зависимостями.
- `pip install pyinstaller pillow`
- Inno Setup 6: `winget install -e --id JRSoftware.InnoSetup`
  (ISCC.exe обычно в `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`).
- Node.js — нужен в рантайме только для Steam-функций сайдкара (кик/проверка входа). См. ниже.

## Сборка (из корня проекта)
```bash
# 0) портативный node.exe для Steam-сайдкара (чтобы работал без установки Node у клиента)
#    копируем системный node.exe (или качаем официальный x64-билд) в packaging/node/
mkdir -p packaging/node && cp "/c/Program Files/nodejs/node.exe" packaging/node/node.exe

# 1) иконка (если менялась)
.venv/Scripts/python packaging/make_icon.py

# 2) заморозить приложение -> packaging/dist/rentbot/rentbot.exe
.venv/Scripts/pyinstaller packaging/rentbot.spec --noconfirm \
    --distpath packaging/dist --workpath packaging/build

# 3) собрать установщик -> packaging/installer_out/rentbot-Setup.exe
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

## Куда что ставится у пользователя
- Приложение: `C:\Program Files\rentbot\` (rentbot.exe + `_internal\` с web, sidecar, зависимостями).
- База сейфа: `%LOCALAPPDATA%\rentbot\rentbot.db` (пишется туда, т.к. Program Files только для чтения;
  при деинсталляции НЕ удаляется — это данные владельца).
- Ярлыки: меню «Пуск» и (опционально) рабочий стол.

## Известные ограничения / дальше
- **Node-сайдкар**: портативный `node.exe` вкладывается в сборку (`packaging/node/` → `_internal/node/`),
  и `rentbot/steam/sidecar.py` (`_node_exe`) при фризе берёт именно его. Поэтому Steam-кик и проверка
  входа работают у клиента БЕЗ установки Node. В dev-режиме используется системный node из PATH.
- **WebView2**: на Windows 10/11 обычно уже установлен (Edge). Если нет — добавить в установщик
  бутстраппер MicrosoftEdgeWebview2Setup.exe.
- **macOS**: сборка DMG требует Mac (PyInstaller + create-dmg). Пока только Windows.
- **Лицензия/активация**: у Pulse — вход через Telegram. У нас первый запуск = setup мастер-пароля;
  активацию через сервер лицензий можно добавить позже (есть Telegram-управление и каркас ЧС-сервера).
