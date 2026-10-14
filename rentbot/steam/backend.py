"""Steam backend (КАРКАС).

guard_code работает уже сейчас (чистый TOTP из shared_secret, см. guard.py).
rotate_credentials (смена пароля + деавторизация всех устройств) — самая хрупкая часть,
её прототип делается на 1-й неделе на собственных аккаунтах.

Два пути реализации (выбираем на прототипе):
  A. Python: ValvePython/steam (вход, confirmations через identity_secret) + поток смены
     пароля на help.steampowered.com. Плюс — один язык. Минус — flow смены пароля меняется.
  B. Node-сайдкар на DoctorMcKay/steam-session + steam-user (зрелые библиотеки),
     вызывается по localhost. Включается, если путь A нестабилен (kill-критерий 10-го дня).

Абстракция SteamBackend (ports.py) позволяет подменить реализацию, не трогая движок.
"""
from __future__ import annotations

import asyncio

from ..ports import SteamBackend
from .guard import generate_code


async def _sidecar_deauth(login: str, password: str, shared_secret: str,
                          proxy: str | None) -> dict:
    """Общая реализация авто-кика через Node-сайдкар (не блокирует event loop).
    «Выйти со всех устройств» без смены пароля — тот же запрос, что кнопка сайта Steam."""
    from . import sidecar
    if not sidecar.available():
        return {"ok": False, "message": "Node-сайдкар не установлен (нужны node + npm install)"}
    if not sidecar.ensure_running():
        return {"ok": False, "message": "Node-сайдкар не запустился"}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: sidecar.deauth(login, password, shared_secret, proxy))


class PySteamBackend(SteamBackend):
    """Путь A. guard_code — готов; rotate_credentials — каркас; deauth — через сайдкар."""

    def guard_code(self, shared_secret: str) -> str:
        return generate_code(shared_secret)

    async def rotate_credentials(self, *, login, password, shared_secret, identity_secret, proxy):
        # TODO (неделя 1, прототип на своих аккаунтах):
        #  1) логин (ValvePython WebAuth) через proxy;
        #  2) запустить смену пароля; подтвердить мобильным аутентификатором (identity_secret);
        #  3) установить новый случайный пароль; деавторизовать все сессии;
        #  4) вернуть новый пароль. При любой ошибке — raise (движок → карантин).
        raise NotImplementedError("Реализуется на прототипе недели 1")

    async def deauth(self, *, login, password, shared_secret, proxy):
        return await _sidecar_deauth(login, password, shared_secret, proxy)


class NodeSidecarBackend(SteamBackend):
    """Путь B. Вызывает локальный Node-сервис (steam-session) по http://127.0.0.1:PORT."""

    def __init__(self, base_url: str = "http://127.0.0.1:8799"):
        self.base_url = base_url

    def guard_code(self, shared_secret: str) -> str:
        return generate_code(shared_secret)

    async def rotate_credentials(self, *, login, password, shared_secret, identity_secret, proxy):
        # TODO: POST {base_url}/rotate c параметрами; вернуть new_password из ответа.
        raise NotImplementedError("Включается, если путь A нестабилен")

    async def deauth(self, *, login, password, shared_secret, proxy):
        return await _sidecar_deauth(login, password, shared_secret, proxy)
