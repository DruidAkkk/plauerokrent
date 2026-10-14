"""Клиент Node-сайдкара Steam (путь B). Запускает `node sidecar/index.js` при необходимости
и ходит к нему по localhost. Используется, когда steampy нестабилен (новый auth-флоу Steam).

Только stdlib (urllib) — ядро не тянет лишних зависимостей.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from urllib import error as urlerror
from urllib import request as urlreq

DEFAULT_PORT = int(os.environ.get("RENTBOT_SIDECAR_PORT", "8799"))


def _sidecar_dir() -> str:
    """Папка Node-сайдкара: из бандла PyInstaller (sys._MEIPASS/sidecar) в установленной
    сборке, иначе — рядом с проектом (dev-режим)."""
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", ""), "sidecar")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "sidecar")


SIDECAR_DIR = _sidecar_dir()


def _base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def is_running(port: int = DEFAULT_PORT, timeout: float = 2.0) -> bool:
    try:
        with urlreq.urlopen(_base(port) + "/health", timeout=timeout) as r:
            return json.loads(r.read()).get("ok") is True
    except Exception:  # noqa: BLE001
        return False


def _node_exe() -> str | None:
    """Путь к node: вложенный портативный node.exe в установленной сборке (sys._MEIPASS/node),
    иначе системный node из PATH. Так Steam-сайдкар работает у клиента без установки Node."""
    if getattr(sys, "frozen", False):
        bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "node", "node.exe")
        if os.path.isfile(bundled):
            return bundled
    return shutil.which("node")


def available() -> bool:
    """Доступны ли node и зависимости сайдкара (можно ли вообще запустить)."""
    return bool(_node_exe()) and \
        os.path.isfile(os.path.join(SIDECAR_DIR, "index.js")) and \
        os.path.isdir(os.path.join(SIDECAR_DIR, "node_modules"))


def ensure_running(port: int = DEFAULT_PORT) -> bool:
    """Поднять сайдкар, если не запущен. Вернуть True, если доступен."""
    if is_running(port):
        return True
    if not available():
        return False
    node = _node_exe()
    env = {**os.environ, "RENTBOT_SIDECAR_PORT": str(port)}
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW — без лишнего окна консоли
    try:
        subprocess.Popen([node, "index.js"], cwd=SIDECAR_DIR, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
    except Exception:  # noqa: BLE001
        return False
    for _ in range(25):  # до ~7.5 c на старт
        if is_running(port):
            return True
        time.sleep(0.3)
    return False


def _post(path: str, payload: dict, port: int, timeout: float) -> dict:
    body = json.dumps(payload).encode()
    req = urlreq.Request(_base(port) + path, data=body,
                        headers={"content-type": "application/json"})
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def login_check(login: str, password: str, shared_secret: str,
                proxy: str | None = None, port: int = DEFAULT_PORT) -> tuple[bool, str]:
    try:
        d = _post("/login-check", {"login": login, "password": password,
                                   "shared_secret": shared_secret, "proxy": proxy}, port, 40)
    except urlerror.URLError as e:
        return False, f"сайдкар недоступен: {e}"
    return bool(d.get("ok")), d.get("message", "")


def deauth(login: str, password: str, shared_secret: str,
           proxy: str | None = None, port: int = DEFAULT_PORT) -> dict:
    """Выкинуть аккаунт со всех устройств (отзыв всех чужих refresh-токенов).

    Пароль НЕ меняется — действие обратимо. Возвращает словарь сайдкара:
    {ok, message, revoked?, total?, failed?, sessions?}. При недоступности сайдкара — ok=False.
    """
    try:
        return _post("/deauth", {"login": login, "password": password,
                                 "shared_secret": shared_secret, "proxy": proxy}, port, 75)
    except urlerror.URLError as e:
        return {"ok": False, "message": f"сайдкар недоступен: {e}"}
