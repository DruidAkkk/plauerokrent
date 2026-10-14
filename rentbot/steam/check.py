"""Пробный вход в Steam — проверка, что логин/пароль/Guard/прокси рабочие.

Не меняет аккаунт (read-only): логинится через steampy и проверяет, что сессия поднялась.
Это та же операция, что бот делает штатно перед управлением аккаунтом, но без побочных действий.
Возвращает (ok: bool, message: str). Сеть — только к Steam (и через прокси аккаунта, если задан).
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile


def _explain(exc: Exception) -> str:
    """Понятное сообщение по типичным сбоям steampy-входа."""
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()
    if name == "InvalidCredentials" or "invalid password" in low or "incorrect" in low:
        return "неверный логин или пароль"
    if name == "CaptchaRequired" or "captcha" in low:
        return "Steam требует CAPTCHA — автоматический вход сейчас невозможен"
    if "no response received from steam" in low:
        return ("Steam не принял вход. Частые причины: неверный пароль, лимит попыток входа "
                "(подождите 10–30 мин), или устаревший метод входа steampy. "
                "Если пароль точно верный — это кандидат на Node-сайдкар (steam-session).")
    if "rate" in low and "limit" in low:
        return "Steam временно ограничил вход (лимит попыток) — подождите и повторите"
    if name == "KeyError" and ("refresh_token" in low or "response" in low):
        return ("Steam не подтвердил вход — код Steam Guard отклонён. Пароль, похоже, верный, "
                "но Guard не прошёл. Проверьте: maFile именно от ЭТОГО аккаунта (сверьте код "
                "кнопкой «Код» с мобильным Steam), и что на аккаунте мобильный аутентификатор, "
                "а не email-Guard. Если maFile точно верный — это случай Node-сайдкара (steam-session).")
    if "guard" in low or ("two" in low and "factor" in low):
        return "проблема со Steam Guard — проверьте shared_secret в maFile и время на ПК"
    return f"{name}: {text[:160]}"


def steam_login_check(login: str, password: str, mafile_json: str | None,
                      proxy: str | None = None) -> tuple[bool, str]:
    from steampy.client import SteamClient

    proxies = {"http": proxy, "https": proxy} if proxy else None
    if not mafile_json:
        return False, "нет maFile — Steam Guard при входе не пройти"

    # базовая проверка maFile: shared_secret должен быть валидным base64 (иначе Guard-код не сгенерить)
    try:
        mf = json.loads(mafile_json)
        ss = mf.get("shared_secret")
        if not ss:
            return False, "в maFile нет shared_secret"
        base64.b64decode(ss)
    except (json.JSONDecodeError, binascii.Error, ValueError):
        return False, "maFile повреждён или shared_secret не base64"

    # ПУТЬ B (приоритет): Node-сайдкар на steam-session — надёжен против нового auth-флоу Steam.
    try:
        from . import sidecar
        if sidecar.ensure_running():
            return sidecar.login_check(login, password, ss, proxy)
    except Exception:  # noqa: BLE001 — сайдкар недоступен → откатываемся на steampy
        pass

    # ПУТЬ A (fallback): steampy. Менее стабилен (может падать KeyError на новом флоу).
    # steampy принимает maFile как путь; JSON-строку на Windows ломает Path.is_file() → пишем во временный файл
    fd, path = tempfile.mkstemp(suffix=".maFile", prefix="rentbot_check_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(mafile_json)
        try:
            client = SteamClient(api_key="", username=login, password=password,
                                 steam_guard=path, proxies=proxies)
        except Exception as e:  # noqa: BLE001 — чаще всего недоступный прокси
            return False, f"прокси/инициализация: {type(e).__name__}: {str(e)[:160]}"
        try:
            client.login()
        except Exception as e:  # noqa: BLE001 — неверный пароль, Guard, капча, лимит Steam
            return False, _explain(e)
        try:
            alive = client.is_session_alive()
        except Exception:  # noqa: BLE001
            alive = False
        if alive:
            return True, "вход выполнен, сессия активна"
        return False, "логин прошёл, но сессия не поднялась"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
