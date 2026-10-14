"""Спайк Steam (безопасная часть) — проверяемо сейчас; разрушающая ротация вынесена отдельно.

Что делает этот скрипт АВТОНОМНО (без риска для аккаунта):
  • парсит maFile (shared_secret, identity_secret, account_name, steamid);
  • генерирует Guard-код и СВЕРЯЕТ его с эталоном steampy (две независимые реализации TOTP);
  • при --login: логинится через прокси (steampy) и подтверждает, что сессия живая. Чтение, не запись.

Чего этот скрипт НЕ делает (и не должен в автономном прогоне):
  • не меняет пароль и не деавторизует сессии. Это единственная необратимая операция в проекте:
    ошибка в середине web-флоу help.steampowered.com → потеря доступа к аккаунту. По брифу это
    «прототип недели 1 на собственных аккаунтах» — выполняется ВРУЧНУЮ, с мобильным
    аутентификатором под рукой. Реализация ротации живёт в rentbot/steam/backend.py и
    запускается осознанно человеком, а не фоновым прогоном.

Переменные окружения:
  STEAM_MAFILE     путь к maFile (обязательно)
  STEAM_LOGIN      логин (иначе account_name из maFile)
  STEAM_PASSWORD   текущий пароль (нужен только для --login)
  STEAM_PROXY      http://user:pass@host:port (весь трафик Steam идёт сюда)

Режимы:
  python spike/steam_check.py --selftest   # синтетический maFile: parse + Guard + сверка. Без сети, без секретов.
  python spike/steam_check.py              # ваш maFile: parse + Guard-код + сверка.
  python spike/steam_check.py --login      # + логин через прокси и проверка живой сессии (чтение).
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from rentbot.steam.guard import generate_code, seconds_left  # noqa: E402
from rentbot.vault import mask  # noqa: E402

REQUIRED_MAFILE_KEYS = ("shared_secret", "identity_secret")


def load_mafile(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    missing = [k for k in REQUIRED_MAFILE_KEYS if not data.get(k)]
    if missing:
        raise ValueError(f"В maFile нет полей: {missing}")
    return data


def account_name(mafile: dict) -> str:
    # В разных maFile логин лежит либо в account_name, либо в account_info.account_name
    return (mafile.get("account_name")
            or (mafile.get("account_info") or {}).get("account_name")
            or "")


def steam_id(mafile: dict) -> str:
    return str(mafile.get("Session", {}).get("SteamID")
               or mafile.get("steamid") or mafile.get("steamID64") or "")


def check_guard(mafile: dict) -> bool:
    """Guard-код из нашей реализации должен совпасть с эталоном steampy."""
    from steampy.guard import generate_one_time_code
    ours = generate_code(mafile["shared_secret"])
    ref = generate_one_time_code(mafile["shared_secret"])
    ok = ours == ref
    print(f"[guard] код={ours}  действителен ещё {seconds_left()} c  "
          f"{'✓ совпал с steampy' if ok else '✗ РАСХОЖДЕНИЕ с steampy: ' + ref}")
    return ok


def do_login(mafile: dict, path: str) -> int:
    """Non-destructive: логин через прокси + проверка живой сессии."""
    login = os.environ.get("STEAM_LOGIN") or account_name(mafile)
    password = os.environ.get("STEAM_PASSWORD")
    proxy = os.environ.get("STEAM_PROXY")
    if not login:
        print("[login] нет логина (STEAM_LOGIN или account_name в maFile)")
        return 2
    if not password:
        print("[login] нет STEAM_PASSWORD — логин невозможен")
        return 2

    from steampy.client import SteamClient
    proxies = {"http": proxy, "https": proxy} if proxy else None
    print(f"[login] {login} через прокси: {'да' if proxy else 'НЕТ (небезопасно для Steam)'}")
    client = SteamClient(api_key="", username=login, password=password,
                         steam_guard=path, proxies=proxies)
    client.login()
    alive = client.is_session_alive()
    print(f"[login] сессия {'ЖИВАЯ' if alive else 'не поднялась'}; steamid={client.get_steam_id()}")
    # Здесь в проде перечисляются активные сессии — чтобы ПОТОМ проверить, что деавторизация сработала.
    return 0 if alive else 1


def make_synthetic_mafile() -> str:
    """Валидный по структуре maFile со случайным секретом — для self-test без реальных данных."""
    ss = base64.b64encode(secrets.token_bytes(20)).decode()
    is_ = base64.b64encode(secrets.token_bytes(20)).decode()
    data = {"shared_secret": ss, "identity_secret": is_,
            "account_name": "selftest_bot", "Session": {"SteamID": "76561190000000000"}}
    fd, p = tempfile.mkstemp(suffix=".maFile", prefix="rentbot_selftest_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return p


def main() -> int:
    try:
        if "--selftest" in sys.argv:
            path = make_synthetic_mafile()
            try:
                mafile = load_mafile(path)
                print(f"[parse] синтетический maFile OK: login={account_name(mafile)} "
                      f"steamid={steam_id(mafile)} shared={mask(mafile['shared_secret'])}")
                ok = check_guard(mafile)
                print("\nSELFTEST:", "OK — parse + Guard + сверка с steampy работают"
                      if ok else "FAIL — коды разошлись")
                return 0 if ok else 1
            finally:
                os.unlink(path)

        path = os.environ.get("STEAM_MAFILE")
        if not path:
            print("Нет STEAM_MAFILE (для проверки без данных: --selftest)")
            return 2
        mafile = load_mafile(path)
        print(f"[parse] maFile OK: login={account_name(mafile)} steamid={steam_id(mafile)} "
              f"shared={mask(mafile['shared_secret'])} identity={mask(mafile['identity_secret'])}")
        if not check_guard(mafile):
            return 1
        if "--login" in sys.argv:
            return do_login(mafile, path)
        print("\nParse + Guard OK. Логин (чтение) — с реальными данными: --login")
        print("Смена пароля/деавторизация — НЕ здесь: это ручной прототип, см. docstring и backend.py")
        return 0
    except Exception as e:
        print(f"\n[FAIL] {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
