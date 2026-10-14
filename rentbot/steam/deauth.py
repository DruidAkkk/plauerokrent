"""«Выйти из аккаунта везде» для Steam-аккаунта (без смены пароля и без удаления аутентификатора).

Логинится как WebBrowser, считает активные сессии (EnumerateTokens) и повторяет действие кнопки
сайта Steam «Выйти из аккаунта везде»: POST /twofactor/manage_action {action:'deauthorize',
sessionid} с веб-куками. Это завершает ВСЕ сессии аккаунта (владельца и арендатора) — вход заново
проходит штатно (пароль + мобильный Guard). Пароль и аутентификатор не трогаются.

Примечание: прямой IAuthenticationService/RevokeRefreshToken по token_id недоступен (требует подпись,
привязанную к секрету самого токена — есть только у устройства-владельца), поэтому кикнуть одну
конкретную чужую сессию нельзя; доступен только аккаунт-уровневый выход со всех устройств.

Работает через Node-сайдкар (steam-session). Возвращает dict:
{ok, supported, revoked, sessions:[...], total, message} или {ok:False, message} при ошибке.
"""
from __future__ import annotations

import base64
import binascii
import json


def steam_deauth(login: str, password: str, mafile_json: str | None,
                 proxy: str | None = None) -> dict:
    if not login or not password:
        return {"ok": False, "supported": False, "message": "нужны логин и пароль аккаунта"}
    if not mafile_json:
        return {"ok": False, "supported": False,
                "message": "нет maFile — без Steam Guard вход не пройти"}

    # базовая проверка maFile: shared_secret должен быть валидным base64
    try:
        mf = json.loads(mafile_json)
        ss = mf.get("shared_secret")
        if not ss:
            return {"ok": False, "supported": False, "message": "в maFile нет shared_secret"}
        base64.b64decode(ss)
    except (json.JSONDecodeError, binascii.Error, ValueError):
        return {"ok": False, "supported": False,
                "message": "maFile повреждён или shared_secret не base64"}

    try:
        from . import sidecar
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "supported": False, "message": f"сайдкар недоступен: {type(e).__name__}"}

    if not sidecar.ensure_running():
        return {"ok": False, "supported": False,
                "message": ("Node-сайдкар недоступен (установите Node.js и выполните "
                            "`npm install` в папке sidecar).")}

    d = sidecar.deauth(login, password, ss, proxy)
    d.setdefault("ok", False)
    d.setdefault("supported", False)
    d.setdefault("sessions", [])
    d.setdefault("message", "неизвестная ошибка")
    return d
