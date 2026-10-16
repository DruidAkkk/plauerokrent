"""Бэкенд десктоп-панели — js_api для pywebview.

Ложится на существующее ядро (rentbot.storage / vault / domain). Главные принципы:
  • Мастер-пароль продавца НЕ хранится: из него выводится ключ (scrypt), в БД лежит только
    соль и «верификатор» (зашифрованная магическая строка) — по нему отличаем неверный пароль
    от битых данных. Пока не разблокировано — ни одно чтение/запись невозможно.
  • В UI НИКОГДА не уходят расшифрованные секреты (пароли, maFile). Наружу — только признаки
    «есть maFile / есть Guard» и маскированные значения.

Все публичные методы (без подчёркивания) вызываются из JS как window.pywebview.api.<method>().
Возвращают JSON-сериализуемые dict'ы вида {"ok": bool, ...} или {"error": "..."}.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from rentbot.domain import Account, AccountState, LotBinding, RentalState, RotationMode
from rentbot.storage import Storage
from rentbot.vault import ENV_SALT, ENV_SECRET, Vault, env_configured

VAULT_MAGIC = "rentbot-vault-v1"       # что шифруем в верификатор
MONTH = 30 * 24 * 3600

STATE_RU = {
    "free": "свободен", "rented": "в аренде", "rotating": "ротация",
    "resting": "отдых", "quarantine": "карантин", "paused": "выключен",
}
RENTAL_STATE_RU = {
    "queued": "в очереди", "active": "активна", "ended": "завершена", "cancelled": "отменена",
}


def _send_tg_login_code(token: str, chat_id: str, code: str) -> None:
    """Отправить код входа в Telegram (sync, curl_cffi). Бросает при ошибке."""
    from curl_cffi import requests
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id,
                            "text": f"🔐 Код входа в rentbot: {code}\nДействует 5 минут. "
                                    "Если это не вы — смените мастер-пароль."},
                      timeout=20)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram вернул ошибку"))


def _api(fn):
    """Обёртка метода js_api: сериализует доступ к БД (pywebview зовёт из разных потоков
    WebView2, подключение SQLite общее) и превращает исключения в {'error': ...},
    чтобы мост JS↔Python и окно не падали."""
    def wrap(self, *a, **kw):
        try:
            with self._lock:
                return fn(self, *a, **kw)
        except Exception as e:  # noqa: BLE001 — любой сбой показываем в UI, не роняем окно
            return {"error": f"{type(e).__name__}: {e}"}
    wrap.__name__ = fn.__name__
    return wrap


class Backend:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.storage: Storage | None = None
        self._worker = None  # rentbot.worker.ThreadedWorker, создаётся при первом запуске
        self._lock = threading.RLock()  # сериализация вызовов js_api (разные потоки WebView2)
        self._pl_auth = None  # rentbot.playerok_auth.PlayerokAuth между запросом и вводом кода
        self._pending = None  # ожидающий вход при 2FA: {storage, code, exp, tries}

    # ---------- локальная сессия («запомнить вход») ----------
    @property
    def _session_path(self) -> str:
        return self.db_path + ".session"

    @property
    def _device_key_path(self) -> str:
        # отдельно от БД: копирование только папки с БД не даёт авто-входа
        return os.path.join(os.path.expanduser("~"), ".rentbot_device.key")

    def _device_key(self) -> bytes:
        from cryptography.fernet import Fernet
        p = self._device_key_path
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read().strip()
        key = Fernet.generate_key()
        with open(p, "wb") as f:
            f.write(key)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return key

    def _save_session(self, master_password: str, username: str, days: int = 30) -> None:
        from cryptography.fernet import Fernet
        blob = Fernet(self._device_key()).encrypt(json.dumps(
            {"mp": master_password, "user": username, "exp": time.time() + days * 86400}).encode())
        with open(self._session_path, "wb") as f:
            f.write(blob)

    def _clear_session(self) -> None:
        try:
            os.remove(self._session_path)
        except OSError:
            pass

    def resume(self) -> dict:
        """Авто-вход с сохранённой локальной сессии (если есть и не истекла). Без 2FA — доверенное устройство."""
        try:
            if self.storage is not None:
                return {"ok": True, "resumed": True}
            if not os.path.exists(self._session_path):
                return {"ok": True, "resumed": False}
            from cryptography.fernet import Fernet
            with open(self._session_path, "rb") as f:
                data = json.loads(Fernet(self._device_key()).decrypt(f.read()))
            if time.time() > data.get("exp", 0):
                self._clear_session()
                return {"ok": True, "resumed": False}
            salt_b64 = self._meta_raw("vault_salt")
            if not salt_b64:
                return {"ok": True, "resumed": False}
            vault = Vault(data["mp"], base64.b64decode(salt_b64))
            if vault.decrypt(self._meta_raw("vault_verifier")) != VAULT_MAGIC:
                raise ValueError
            with self._lock:
                self.storage = Storage(self.db_path, vault)
            return {"ok": True, "resumed": True, "username": data.get("user", "")}
        except Exception:  # noqa: BLE001 — любая проблема с сессией → обычный вход
            self._clear_session()
            return {"ok": True, "resumed": False}

    # ---------- служебное ----------
    def _meta_raw(self, key: str) -> str | None:
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
            row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def _require(self) -> Storage:
        if self.storage is None:
            raise RuntimeError("Хранилище заблокировано — введите мастер-пароль")
        return self.storage

    # ---------- статус / разблокировка ----------
    @_api
    def status(self) -> dict:
        return {
            "ok": True,
            "initialized": self._meta_raw("vault_salt") is not None,
            "unlocked": self.storage is not None,
            "env_mode": env_configured(),   # ключ из env (ENCRYPTION_SECRET/SALT) → вход без пароля
            "username": self._meta_raw("username") or "",
            "worker": bool(self._worker and self._worker.is_running()),
            "worker_error": getattr(self._worker, "last_error", None),
            "db_path": self.db_path,
        }

    def env_unlock(self) -> dict:
        """Открыть хранилище ключом из env (для панели на той же VPS, что и воркер). Без пароля.
        НЕ @_api: сам ставит self.storage, _require ещё недоступен."""
        with self._lock:
            if self.storage is not None:
                return {"ok": True}
            if not env_configured():
                return {"error": f"env-ключ не задан (нужны {ENV_SECRET} и {ENV_SALT})"}
            try:
                self.storage = Storage(self.db_path, Vault.from_env())
            except Exception as e:  # noqa: BLE001
                return {"error": f"env-ключ: {type(e).__name__}: {e}"}
            self.storage.log("env_unlock")
            return {"ok": True}

    @_api
    def setup(self, master_password: str, username: str = "", remember: bool = False) -> dict:
        if not master_password or len(master_password) < 4:
            return {"error": "Мастер-пароль слишком короткий (минимум 4 символа)"}
        if self._meta_raw("vault_salt") is not None:
            return {"error": "Хранилище уже создано — используйте разблокировку"}
        salt = Vault.new_salt()
        vault = Vault(master_password, salt)
        storage = Storage(self.db_path, vault)
        storage.db.execute("INSERT OR REPLACE INTO meta VALUES ('vault_salt', ?)",
                           (base64.b64encode(salt).decode(),))
        storage.db.execute("INSERT OR REPLACE INTO meta VALUES ('vault_verifier', ?)",
                           (vault.encrypt(VAULT_MAGIC),))
        storage.db.execute("INSERT OR REPLACE INTO meta VALUES ('username', ?)",
                           ((username or "").strip(),))
        self.storage = storage
        if remember:
            self._save_session(master_password, (username or "").strip())
        return {"ok": True}

    def unlock(self, master_password: str, username: str = "", remember: bool = False) -> dict:
        """Вход: логин + мастер-пароль (+ 2FA через Telegram, если включено).
        НЕ декорирован _api: отправка кода в TG идёт вне self._lock. При 2FA доступ выдаётся
        не сразу, а после verify_2fa. remember — сохранить сессию локально (не логиниться каждый раз)."""
        try:
            with self._lock:
                salt_b64 = self._meta_raw("vault_salt")
                if salt_b64 is None:
                    return {"error": "Хранилище ещё не создано", "need_setup": True}
                stored_user = self._meta_raw("username") or ""
                # если логин задан — он обязателен; старые БД без логина не ломаем
                if stored_user and (username or "").strip() != stored_user:
                    return {"error": "Неверный логин или пароль"}
                vault = Vault(master_password, base64.b64decode(salt_b64))
                verifier = self._meta_raw("vault_verifier")
                try:
                    if vault.decrypt(verifier) != VAULT_MAGIC:
                        raise ValueError
                except Exception:
                    return {"error": "Неверный логин или пароль"}
                storage = Storage(self.db_path, vault)
                twofa = storage.get_setting("login_2fa_enabled") == "1"
                tg_token = storage.get_setting("tg_token")
                tg_chat = storage.get_setting("tg_chat")
            eff_user = stored_user or (username or "").strip()

            if not twofa:
                with self._lock:
                    self.storage = storage
                if remember:
                    self._save_session(master_password, eff_user)
                return {"ok": True}
            if not (tg_token and tg_chat):
                # 2FA включена, но TG не настроен — не можем подтвердить. Пускаем с предупреждением.
                with self._lock:
                    self.storage = storage
                if remember:
                    self._save_session(master_password, eff_user)
                return {"ok": True, "warning": "2FA включена, но Telegram не настроен — вход без кода"}

            code = f"{secrets.randbelow(1000000):06d}"
            r = self._run_net(lambda: _send_tg_login_code(tg_token, tg_chat, code))
            if r.get("error"):
                try:
                    storage.db.close()
                except Exception:
                    pass
                return {"error": f"Не удалось отправить код в Telegram: {r['error']}"}
            self._pending = {"storage": storage, "code": code, "exp": time.time() + 300,
                             "tries": 0, "remember": remember, "password": master_password,
                             "username": eff_user}
            return {"ok": False, "need_2fa": True, "message": "Код отправлен в Telegram"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    def verify_2fa(self, code: str) -> dict:
        """Подтвердить код из Telegram и выдать доступ."""
        p = self._pending
        if not p:
            return {"error": "Нет ожидающего входа — войдите заново"}
        if time.time() > p["exp"]:
            self._pending = None
            try:
                p["storage"].db.close()
            except Exception:
                pass
            return {"error": "Код истёк — войдите заново"}
        p["tries"] += 1
        if p["tries"] > 5:
            self._pending = None
            try:
                p["storage"].db.close()
            except Exception:
                pass
            return {"error": "Слишком много попыток — войдите заново"}
        if (code or "").strip() != p["code"]:
            return {"error": "Неверный код"}
        with self._lock:
            self.storage = p["storage"]
            self.storage.log("login_2fa_ok")
        if p.get("remember"):
            self._save_session(p["password"], p.get("username", ""))
        self._pending = None
        return {"ok": True}

    @_api
    def lock(self) -> dict:
        if self._worker and self._worker.is_running():
            self._worker.stop()
        if self.storage is not None:
            try:
                self.storage.db.close()
            except Exception:
                pass
        self.storage = None
        self._clear_session()  # явная блокировка = забыть сохранённый вход
        if self._pending:
            try:
                self._pending["storage"].db.close()
            except Exception:
                pass
            self._pending = None
        return {"ok": True}

    # ---------- безопасность входа (логин + 2FA) ----------
    @_api
    def security_get(self) -> dict:
        s = self._require()
        return {"ok": True,
                "username": self._meta_raw("username") or "",
                "twofa_enabled": s.get_setting("login_2fa_enabled") == "1",
                "tg_ready": bool(s.get_setting("tg_token") and s.get_setting("tg_chat"))}

    @_api
    def security_set(self, data: dict) -> dict:
        s = self._require()
        if "username" in data:
            s.db.execute("INSERT OR REPLACE INTO meta VALUES ('username', ?)",
                         ((data.get("username") or "").strip(),))
        if "twofa_enabled" in data:
            on = bool(data["twofa_enabled"])
            if on and not (s.get_setting("tg_token") and s.get_setting("tg_chat")):
                return {"error": "Сначала настройте Telegram (токен бота + Chat ID) в этом разделе ниже"}
            s.set_setting("login_2fa_enabled", "1" if on else "0")
        return {"ok": True}

    # ---------- дашборд ----------
    @_api
    def dashboard(self) -> dict:
        s = self._require()
        now = time.time()
        accounts = s.list_accounts()
        by_state: dict[str, int] = {}
        for a in accounts:
            by_state[a.state.value] = by_state.get(a.state.value, 0) + 1
        st = s.stats(since=now - MONTH)

        # сводка «сегодня» по всем плагинам (Steam-аренда + KoSell + PUBG Mobile)
        lt = time.localtime()
        since_today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        st_today = s.stats(since=since_today)
        ko = s.kosell_rental_list(limit=500)
        mob = s.mobile_rental_list(limit=500)
        ko_today = sum(1 for r in ko if (r.get("created") or 0) >= since_today)
        mob_today = sum(1 for r in mob if (r.get("started") or 0) >= since_today)
        ko_active = sum(1 for r in ko if r.get("status") == "active")
        mob_active = sum(1 for r in mob if r.get("status") == "active")

        return {
            "ok": True,
            "revenue_month": round(st["revenue"], 2),
            "rentals_month": st["rentals"],
            "active": len(s.rentals_by_state(RentalState.ACTIVE)),
            "queued": len(s.rentals_by_state(RentalState.QUEUED)),
            "accounts_total": len(accounts),
            "accounts_by_state": {STATE_RU.get(k, k): v for k, v in by_state.items()},
            "quarantine": by_state.get("quarantine", 0),
            "utilization": st["accounts"],  # [{login, rentals, revenue, utilization}]
            "today": {
                "total": st_today["rentals"] + ko_today + mob_today,
                "steam": st_today["rentals"], "steam_revenue": round(st_today["revenue"], 2),
                "kosell": ko_today, "mobile": mob_today,
            },
            "plugins": {
                "kosell": {"enabled": bool(s.get_setting("kosell_key")),
                           "today": ko_today, "active": ko_active},
                "mobile": {"today": mob_today, "active": mob_active,
                           "free": s.mobile_free_count("pubg_mobile"), "pool": len(s.mobile_list())},
            },
            "warranty": {
                "enabled": s.get_setting("warranty_enabled") != "0",
                "open": sum(1 for w in s.warranty_list(limit=500) if w.get("status") == "open"),
            },
        }

    # ---------- CRM покупателей (Steam-аренда) ----------
    @_api
    def buyers_crm(self) -> dict:
        s = self._require()
        items = s.buyers_crm("playerok")
        for it in items:
            it["blacklisted"] = s.is_blacklisted("steam", "playerok", it["buyer_id"])
        return {"ok": True, "items": items}

    # ---------- экспорт продаж в CSV ----------
    def _save_dialog(self, default_name: str) -> str | None:
        """Нативный диалог «Сохранить». None — пользователь отменил. При недоступности окна
        (напр. не pywebview) — путь рядом с БД, чтобы экспорт всё равно сработал."""
        try:
            import webview
            wins = getattr(webview, "windows", None)
            if wins:
                res = wins[0].create_file_dialog(
                    webview.SAVE_DIALOG, save_filename=default_name, file_types=("CSV (*.csv)",))
                if not res:
                    return None  # отмена
                return res if isinstance(res, str) else res[0]
        except Exception:  # noqa: BLE001 — падение диалога не должно ронять экспорт
            pass
        return os.path.join(os.path.dirname(self.db_path) or ".", default_name)

    @_api
    def export_sales_csv(self) -> dict:
        from rentbot import export
        s = self._require()
        rows = export.sales_rows(s)
        if not rows:
            return {"error": "Пока нет продаж для экспорта"}
        default_name = time.strftime("rentbot_sales_%Y%m%d_%H%M.csv")
        path = self._save_dialog(default_name)
        if not path:
            return {"ok": False, "cancelled": True}
        if not path.lower().endswith(".csv"):
            path += ".csv"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(export.sales_csv(s))
        s.log("export_sales", data={"rows": len(rows), "path": os.path.basename(path)})
        return {"ok": True, "path": path, "rows": len(rows)}

    # ---------- аккаунты ----------
    def _acc_public(self, a: Account) -> dict:
        """Безопасная проекция аккаунта для UI — без расшифрованных секретов."""
        return {
            "id": a.id, "game": a.game, "login": a.login,
            "state": a.state.value, "state_ru": STATE_RU.get(a.state.value, a.state.value),
            "proxy": a.proxy or "", "note": a.note, "tags": a.tags,
            "has_guard": bool(a.shared_secret), "has_mafile": bool(a.mafile_json),
            "rest_until": a.rest_until,
        }

    @_api
    def accounts(self) -> dict:
        s = self._require()
        return {"ok": True, "items": [self._acc_public(a) for a in s.list_accounts()]}

    @_api
    def add_account(self, data: dict) -> dict:
        s = self._require()
        login = (data.get("login") or "").strip()
        game = (data.get("game") or "").strip()
        password = data.get("password") or ""
        if not (login and game and password):
            return {"error": "Нужны логин, игра и пароль"}

        shared = identity = mafile_json = None
        raw_mafile = data.get("mafile")  # строка с JSON maFile (необязательно)
        if raw_mafile:
            try:
                mf = json.loads(raw_mafile)
            except json.JSONDecodeError:
                return {"error": "maFile — не валидный JSON"}
            shared = mf.get("shared_secret")
            identity = mf.get("identity_secret")
            if not shared:
                return {"error": "В maFile нет shared_secret"}
            mafile_json = raw_mafile

        acc = Account(
            id=None, game=game, login=login, password=password,
            shared_secret=shared, identity_secret=identity, mafile_json=mafile_json,
            proxy=(data.get("proxy") or "").strip() or None,
            note=(data.get("note") or "").strip(),
            tags=[t.strip() for t in (data.get("tags") or "").split(",") if t.strip()],
        )
        try:
            acc_id = s.add_account(acc)
        except sqlite3.IntegrityError:
            return {"error": f"Аккаунт с логином «{login}» уже есть"}
        s.log("account_added", account_id=acc_id)
        return {"ok": True, "id": acc_id}

    @_api
    def account_check(self, account_id: int) -> dict:
        """Проверка аккаунта: сгенерировать живой Steam Guard-код из shared_secret.
        Если код генерируется — maFile/секрет валиден, бот сможет выдавать коды по !код.
        Наружу уходит только эфемерный код (меняется каждые 30 c), пароль — нет."""
        s = self._require()
        acc = s.get_account(int(account_id))
        if not acc.shared_secret:
            return {"ok": True, "has_guard": False, "login": acc.login,
                    "message": "У аккаунта нет shared_secret (maFile не задан) — "
                               "Guard-коды бот выдавать не сможет."}
        from rentbot.steam.guard import generate_code, seconds_left
        try:
            code = generate_code(acc.shared_secret)
        except Exception as e:  # noqa: BLE001
            return {"error": f"shared_secret некорректен — код не сгенерировать: {e}"}
        return {"ok": True, "has_guard": True, "login": acc.login,
                "code": code, "seconds_left": seconds_left()}

    @_api
    def account_credentials(self, account_id: int) -> dict:
        """Показать владельцу логин+пароль аккаунта (расшифровка из сейфа по явному действию).
        Сейф уже открыт мастер-паролем; это как «показать пароль» в менеджере паролей. В логи
        пишем только факт просмотра, не значения."""
        s = self._require()
        acc = s.get_account(int(account_id))
        s.log("creds_viewed", account_id=int(account_id))
        return {"ok": True, "login": acc.login, "password": acc.password or "",
                "has_mafile": bool(acc.mafile_json)}

    def account_login_check(self, account_id: int) -> dict:
        """Пробный вход в Steam (логин+пароль+Guard+прокси). НЕ декорирован _api намеренно:
        сетевой вход идёт БЕЗ удержания self._lock, иначе панель замёрзла бы на ~30 c.
        Данные читаем под локом (быстро), сеть — вне лока, с таймаутом."""
        try:
            with self._lock:
                acc = self._require().get_account(int(account_id))
                login, password = acc.login, acc.password
                mafile, proxy = acc.mafile_json, acc.proxy
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        if not password:
            return {"ok": True, "login_ok": False, "message": "у аккаунта не задан пароль"}
        if not mafile:
            return {"ok": True, "login_ok": False, "message": "нет maFile — Guard при входе не пройти"}

        result: dict = {}

        def run():
            from rentbot.steam import check
            try:
                ok, msg = check.steam_login_check(login, password, mafile, proxy)
                result["ok"], result["msg"] = ok, msg
            except Exception as e:  # noqa: BLE001
                result["ok"], result["msg"] = False, f"{type(e).__name__}: {str(e)[:160]}"

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(timeout=60)
        if th.is_alive():
            return {"ok": True, "login_ok": False,
                    "message": "таймаут входа (>60 c) — вероятно, проблема с прокси или сетью"}
        with self._lock:
            if self.storage is not None:
                self.storage.log("login_check", account_id=int(account_id),
                                 data={"ok": bool(result.get("ok")), "msg": result.get("msg", "")})
        return {"ok": True, "login_ok": bool(result.get("ok")), "message": result.get("msg", "")}

    def account_deauth(self, account_id: int) -> dict:
        """«Выйти из аккаунта везде» для Steam-аккаунта (без смены пароля/удаления аутентификатора).
        Повторяет действие кнопки сайта Steam (POST /twofactor/manage_action action=deauthorize) —
        завершает все сессии владельца и арендатора. Не декорирован _api: сетевой вход идёт ВНЕ
        self._lock (иначе панель замёрзла бы). Данные читаем под локом, сеть — вне."""
        try:
            with self._lock:
                acc = self._require().get_account(int(account_id))
                login, password = acc.login, acc.password
                mafile, proxy = acc.mafile_json, acc.proxy
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        if not password:
            return {"ok": False, "supported": False, "message": "у аккаунта не задан пароль"}
        if not mafile:
            return {"ok": False, "supported": False, "message": "нет maFile — без Guard вход не пройти"}

        result: dict = {}

        def run():
            from rentbot.steam import deauth as deauth_mod
            try:
                result["d"] = deauth_mod.steam_deauth(login, password, mafile, proxy)
            except Exception as e:  # noqa: BLE001
                result["d"] = {"ok": False, "supported": False,
                               "message": f"{type(e).__name__}: {str(e)[:160]}"}

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(timeout=90)
        if th.is_alive():
            return {"ok": False, "supported": False,
                    "message": "таймаут (>90 c) — проблема с прокси/сетью"}
        d = result.get("d") or {"ok": False, "supported": False, "message": "нет ответа"}
        with self._lock:
            if self.storage is not None:
                self.storage.log("deauth", account_id=int(account_id),
                                 data={"ok": bool(d.get("ok")), "n": len(d.get("sessions") or []),
                                       "msg": d.get("message", "")})
        return d

    @_api
    def account_update(self, data: dict) -> dict:
        """Настройки аккаунта: игра, прокси, теги, заметка, состояние; опц. смена пароля/maFile.
        Логин не меняется (уникальный ключ). Пустые пароль/maFile — оставить как есть."""
        s = self._require()
        aid = int(data.get("account_id"))
        acc = s.get_account(aid)  # KeyError → обёртка вернёт ошибку
        game = (data.get("game") or acc.game).strip()
        if not game:
            return {"error": "Игра/метка пула не может быть пустой"}
        proxy = (data.get("proxy") or "").strip() or None
        note = (data.get("note") or "").strip()
        tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]
        s.update_account_fields(aid, game, proxy, note, tags)

        if data.get("state"):
            try:
                st = AccountState(data["state"])
            except ValueError:
                return {"error": f"Неизвестное состояние: {data['state']}"}
            s.set_account_state(aid, st, rest_until=0.0)

        if data.get("password"):
            s.set_password(aid, data["password"])

        if data.get("mafile"):
            try:
                mf = json.loads(data["mafile"])
            except json.JSONDecodeError:
                return {"error": "maFile — не валидный JSON"}
            if not mf.get("shared_secret"):
                return {"error": "В maFile нет shared_secret"}
            s.update_account_secrets(aid, mf.get("shared_secret"), mf.get("identity_secret"),
                                     data["mafile"])

        s.log("account_updated", account_id=aid)
        return {"ok": True}

    @_api
    def account_delete(self, account_id: int) -> dict:
        """Удалить аккаунт из пула. Нельзя, если он сейчас в аренде/ротации."""
        s = self._require()
        acc = s.get_account(int(account_id))
        if acc.state in (AccountState.RENTED, AccountState.ROTATING):
            return {"error": f"Нельзя удалить: аккаунт сейчас «{STATE_RU.get(acc.state.value, acc.state.value)}». "
                             "Дождитесь конца аренды/ротации или остановите воркер."}
        s.delete_account(int(account_id))
        s.log("account_deleted", data={"login": acc.login})
        return {"ok": True}

    @_api
    def set_account_state(self, account_id: int, state: str) -> dict:
        s = self._require()
        try:
            st = AccountState(state)
        except ValueError:
            return {"error": f"Неизвестное состояние: {state}"}
        s.set_account_state(account_id, st)
        s.log("account_state", account_id=account_id, state=state)
        return {"ok": True}

    # ---------- лоты ----------
    @_api
    def lots(self) -> dict:
        s = self._require()
        games = {a.game for a in s.list_accounts()}
        out = []
        for g in sorted(games):
            for lot in s.lots_for_game(g):
                out.append({"marketplace": lot.marketplace, "lot_id": lot.lot_id,
                            "game": lot.game, "hours": lot.hours,
                            "rotation": lot.rotation.value, "title": lot.title})
        return {"ok": True, "items": out}

    @_api
    def bind_lot(self, data: dict) -> dict:
        s = self._require()
        try:
            lot = LotBinding(
                marketplace=(data.get("marketplace") or "playerok").strip(),
                lot_id=(data.get("lot_id") or "").strip(),
                game=(data.get("game") or "").strip(),
                hours=float(data.get("hours") or 0),
                rotation=RotationMode(data.get("rotation") or "auto"),
                title=(data.get("title") or "").strip(),
            )
        except (ValueError, TypeError) as e:
            return {"error": f"Проверьте поля лота: {e}"}
        if not (lot.lot_id and lot.game and lot.hours > 0):
            return {"error": "Нужны lot_id, игра и срок аренды (часы) > 0"}
        s.bind_lot(lot)
        return {"ok": True}

    # ---------- аренды ----------
    def _rent_public(self, r) -> dict:
        return {
            "id": r.id, "deal_id": r.deal_id, "lot_id": r.lot_id,
            "buyer_name": r.buyer_name, "account_id": r.account_id,
            "state": r.state.value, "state_ru": RENTAL_STATE_RU.get(r.state.value, r.state.value),
            "started_at": r.started_at, "ends_at": r.ends_at, "price": r.price,
        }

    @_api
    def rentals(self) -> dict:
        s = self._require()
        out = {}
        for st in (RentalState.ACTIVE, RentalState.QUEUED, RentalState.ENDED, RentalState.CANCELLED):
            out[st.value] = [self._rent_public(r) for r in s.rentals_by_state(st)]
        return {"ok": True, "groups": out}

    # ---------- чёрный список (у каждой аренды свой: scope = steam | kosell | mobile) ----------
    _BL_SCOPES = ("steam", "kosell", "mobile")

    def _bl_scope(self, scope) -> str:
        return scope if scope in self._BL_SCOPES else "steam"

    @_api
    def blacklist_list(self, scope: str = "steam") -> dict:
        s = self._require()
        return {"ok": True, "scope": self._bl_scope(scope),
                "items": s.blacklist_list(self._bl_scope(scope))}

    @_api
    def blacklist_add(self, data: dict) -> dict:
        s = self._require()
        bid = (data.get("buyer_id") or "").strip()
        if not bid:
            return {"error": "Нужен ID (или имя) покупателя"}
        reason = (data.get("reason") or "").strip() or "без причины"
        scope = self._bl_scope(data.get("scope"))
        s.blacklist_add(scope, data.get("marketplace") or "playerok", bid, reason)
        s.log("blacklist_add", data={"scope": scope, "buyer": bid, "reason": reason})
        return {"ok": True}

    @_api
    def blacklist_remove(self, scope: str, marketplace: str, buyer_id: str) -> dict:
        s = self._require()
        sc = self._bl_scope(scope)
        s.blacklist_remove(sc, marketplace or "playerok", str(buyer_id))
        s.log("blacklist_remove", data={"scope": sc, "buyer": buyer_id})
        return {"ok": True}

    # ---------- настройки аренды (плагин) ----------
    @_api
    def rental_settings_get(self) -> dict:
        s = self._require()
        return {"ok": True,
                "bonus_5star_hours": float(s.get_setting("rental_bonus_5star_hours", "0") or 0),
                "autokick": s.get_setting("steam_autokick") != "0"}

    @_api
    def rental_settings_set(self, data: dict) -> dict:
        s = self._require()
        if "autokick" in data:
            s.set_setting("steam_autokick", "1" if data.get("autokick") else "0")
        if "bonus_5star_hours" not in data:
            return {"ok": True}
        try:
            b = float(data.get("bonus_5star_hours") or 0)
        except (ValueError, TypeError):
            return {"error": "Бонус-часы — число"}
        if not (0 <= b <= 24):
            return {"error": "Бонус-часы: от 0 до 24"}
        s.set_setting("rental_bonus_5star_hours", str(b))
        return {"ok": True}

    # ---------- логи аренды (отфильтрованные события) ----------
    @_api
    def rental_events(self, limit: int = 200) -> dict:
        s = self._require()
        kinds = ("issue", "code", "extend", "end", "rotated", "rotate_fail",
                 "queued", "rotated_manual")
        ph = ",".join("?" * len(kinds))
        rows = s.db.execute(
            f"SELECT ts, kind, rental_id, account_id, data FROM events WHERE kind IN ({ph}) "
            "ORDER BY id DESC LIMIT ?", (*kinds, int(limit))).fetchall()
        return {"ok": True, "items": [
            {"ts": r["ts"], "kind": r["kind"], "rental_id": r["rental_id"],
             "account_id": r["account_id"], "data": r["data"]} for r in rows]}

    # ---------- журнал событий ----------
    @_api
    def events(self, limit: int = 200) -> dict:
        s = self._require()
        rows = s.db.execute(
            "SELECT ts, kind, rental_id, account_id, data FROM events ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
        return {"ok": True, "items": [
            {"ts": r["ts"], "kind": r["kind"], "rental_id": r["rental_id"],
             "account_id": r["account_id"], "data": r["data"]} for r in rows]}

    # ---------- автовыдача ----------
    @_api
    def delivery_list(self) -> dict:
        s = self._require()
        items = s.list_delivery()
        for d in items:
            if d["mode"] == "keys":
                d["stock"] = s.stock_count(d["marketplace"], d["lot_id"])
        return {"ok": True, "items": items}

    @_api
    def delivery_set(self, data: dict) -> dict:
        s = self._require()
        lot_id = (data.get("lot_id") or "").strip()
        mode = data.get("mode")
        if not lot_id or mode not in ("text", "keys", "file"):
            return {"error": "Нужен lot_id и режим (text/keys/file)"}
        if mode in ("text", "file") and not (data.get("template") or "").strip():
            return {"error": "Для text/file нужен шаблон/путь к файлу"}
        s.set_delivery(data.get("marketplace") or "playerok", lot_id, mode,
                       (data.get("template") or "").strip(), bool(data.get("enabled", True)))
        return {"ok": True}

    @_api
    def stock_add(self, marketplace: str, lot_id: str, payloads_text: str) -> dict:
        s = self._require()
        lines = [x for x in (payloads_text or "").splitlines() if x.strip()]
        n = s.add_stock(marketplace or "playerok", lot_id, lines)
        s.log("stock_added", data={"lot": lot_id, "n": n})
        return {"ok": True, "added": n, "stock": s.stock_count(marketplace or "playerok", lot_id)}

    # ---------- автоответчик ----------
    @_api
    def autoresponses_list(self) -> dict:
        return {"ok": True, "items": self._require().list_autoresponses()}

    @_api
    def autoresponse_add(self, data: dict) -> dict:
        s = self._require()
        kind = data.get("kind")
        reply = (data.get("reply") or "").strip()
        if kind not in ("greeting", "keyword") or not reply:
            return {"error": "Нужен тип (greeting/keyword) и текст ответа"}
        if kind == "keyword" and not (data.get("pattern") or "").strip():
            return {"error": "Для правила по ключевому слову нужен паттерн"}
        rid = s.add_autoresponse(kind, reply, (data.get("pattern") or "").strip())
        return {"ok": True, "id": rid}

    @_api
    def autoresponse_delete(self, rid: int) -> dict:
        self._require().delete_autoresponse(int(rid))
        return {"ok": True}

    # ---------- автоподнятие ----------
    @_api
    def bump_list(self) -> dict:
        return {"ok": True, "items": self._require().list_bump()}

    @_api
    def bump_set(self, data: dict) -> dict:
        s = self._require()
        lot_id = (data.get("lot_id") or "").strip()
        if not lot_id:
            return {"error": "Нужен lot_id"}
        try:
            interval_min = float(data.get("interval_min") or 0)
        except (ValueError, TypeError):
            return {"error": "Интервал должен быть числом (минуты)"}
        if bool(data.get("enabled")) and interval_min < 30:
            return {"error": "Антибан: минимальный интервал автоподнятия — 30 минут"}
        s.set_bump(data.get("marketplace") or "playerok", lot_id,
                   bool(data.get("enabled")), interval_min * 60)
        return {"ok": True}

    # ---------- просьба об отзыве (после успешной сделки) ----------
    @_api
    def review_get(self) -> dict:
        from rentbot import review_request as rr
        s = self._require()
        return {"ok": True,
                "enabled": s.get_setting(rr.ENABLED_KEY) == "1",
                "text": s.get_setting(rr.TEXT_KEY) or "",
                "default": rr.TEMPLATE}

    @_api
    def review_set(self, data: dict) -> dict:
        from rentbot import review_request as rr
        s = self._require()
        s.set_setting(rr.ENABLED_KEY, "1" if data.get("enabled") else "0")
        if "text" in data:
            s.set_setting(rr.TEXT_KEY, (data.get("text") or "").strip())
        return {"ok": True}

    # ---------- настройки (Telegram) ----------
    @staticmethod
    def _mask_tok(tok: str | None) -> str:
        if not tok:
            return ""
        return (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else "***"

    @_api
    def settings_get(self) -> dict:
        s = self._require()
        tg = s.get_setting("tg_token")
        pl = s.get_setting("pl_token")
        return {"ok": True,
                "tg_token_set": bool(tg), "tg_token_mask": self._mask_tok(tg),
                "tg_chat": s.get_setting("tg_chat") or "",
                "pl_token_set": bool(pl), "pl_token_mask": self._mask_tok(pl),
                "pl_proxy": s.get_setting("pl_proxy") or "",
                "pl_auth_method": s.get_setting("pl_auth_method") or "cookie",
                "pl_email": s.get_setting("pl_email") or ""}

    @_api
    def settings_set(self, data: dict) -> dict:
        s = self._require()
        if data.get("tg_token"):
            s.set_setting("tg_token", data["tg_token"].strip(), secret=True)
        if "tg_chat" in data:
            s.set_setting("tg_chat", (data["tg_chat"] or "").strip())
        if data.get("pl_token"):
            s.set_setting("pl_token", data["pl_token"].strip(), secret=True)
            s.set_setting("pl_auth_method", "cookie")
        if "pl_proxy" in data:
            s.set_setting("pl_proxy", (data["pl_proxy"] or "").strip() or None)
        if data.get("pl_auth_method") in ("cookie", "email"):
            s.set_setting("pl_auth_method", data["pl_auth_method"])
        return {"ok": True}

    # ---------- плагин KoSell (реселл почасовой аренды Steam) ----------
    def _kosell_params(self) -> tuple[str | None, str | None]:
        with self._lock:
            s = self._require()
            return s.get_setting("kosell_key"), s.get_setting("kosell_proxy")

    def _kosell_net(self, fn, timeout: float = 40.0) -> dict:
        """Сетевой вызов KoSell ВНЕ self._lock (не морозит UI). Ключ читаем под локом."""
        key, proxy = self._kosell_params()
        if not key:
            return {"error": "API-ключ KoSell не задан (Настройки → KoSell)"}

        def call():
            from rentbot.adapters.kosell import KoSellClient, KoSellError
            c = KoSellClient(key, proxy)
            try:
                return fn(c)
            except KoSellError as e:
                raise RuntimeError(e.ru)  # noqa: B904 — отдаём человекочитаемый текст

        return self._run_net(call, timeout=timeout)

    @staticmethod
    def _kosell_err(r: dict) -> str:
        return (r.get("error") or "").removeprefix("RuntimeError: ")

    @_api
    def kosell_settings_get(self) -> dict:
        s = self._require()
        key = s.get_setting("kosell_key")
        return {"ok": True, "key_set": bool(key), "key_mask": self._mask_tok(key),
                "currency": s.get_setting("kosell_currency") or "RUB",
                "markup": s.get_setting("kosell_markup") or "100",
                "proxy": s.get_setting("kosell_proxy") or ""}

    @_api
    def kosell_settings_set(self, data: dict) -> dict:
        s = self._require()
        if data.get("key"):
            s.set_setting("kosell_key", data["key"].strip(), secret=True)
        if data.get("currency") in ("RUB", "USD"):
            s.set_setting("kosell_currency", data["currency"])
        if "markup" in data:
            try:
                mk = max(0.0, float(data.get("markup") or 100))
            except (TypeError, ValueError):
                return {"error": "Наценка должна быть числом"}
            s.set_setting("kosell_markup", str(mk))
        if "proxy" in data:
            s.set_setting("kosell_proxy", (data["proxy"] or "").strip() or None)
        return {"ok": True}

    def kosell_check(self) -> dict:
        r = self._kosell_net(lambda c: c.check())
        if "error" in r:
            return {"ok": True, "connected": False, "message": self._kosell_err(r)}
        ok, msg = r["val"]
        return {"ok": True, "connected": ok, "message": msg}

    def kosell_balance(self) -> dict:
        r = self._kosell_net(lambda c: c.balance())
        if "error" in r:
            return {"ok": False, "message": self._kosell_err(r)}
        return {"ok": True, **r["val"]}

    def kosell_products(self) -> dict:
        r = self._kosell_net(lambda c: c.products(), timeout=30)
        if "error" in r:
            return {"ok": False, "message": self._kosell_err(r)}
        items = r["val"] or []
        slim = [{"id": p["id"], "name": p["name"],
                 "price_per_hour_rub": p.get("price_per_hour_rub"),
                 "price_per_hour_usd": p.get("price_per_hour_usd"),
                 "available": p.get("available_accounts", 0),
                 "min_hours": p.get("min_hours", 1), "max_hours": p.get("max_hours", 720)}
                for p in items]
        return {"ok": True, "items": slim, "count": len(slim)}

    def kosell_active(self) -> dict:
        """Активные аренды на аккаунте KoSell (их сторона)."""
        r = self._kosell_net(lambda c: c.active())
        if "error" in r:
            return {"ok": False, "message": self._kosell_err(r)}
        return {"ok": True, "items": r["val"] or []}

    @_api
    def kosell_map_list(self) -> dict:
        return {"ok": True, "items": self._require().kosell_map_list()}

    @_api
    def kosell_map_set(self, data: dict) -> dict:
        s = self._require()
        try:
            product_id = int(data["product_id"])
            hours = max(1, int(data.get("hours") or 24))
        except (KeyError, TypeError, ValueError):
            return {"error": "Нужны product_id и часы (целые числа)"}
        lot_id = (data.get("lot_id") or "").strip()
        if not lot_id:
            return {"error": "Укажите id лота Playerok"}
        try:
            markup = max(0.0, float(data.get("markup_percent") or 100))
        except (TypeError, ValueError):
            return {"error": "Наценка должна быть числом"}
        currency = data.get("currency") if data.get("currency") in ("RUB", "USD") else "RUB"
        s.kosell_map_set(data.get("marketplace") or "playerok", lot_id, product_id,
                         (data.get("product_name") or "").strip(), hours, markup, currency,
                         bool(data.get("enabled", True)))
        s.log("kosell_map_set", data={"lot_id": lot_id, "product_id": product_id})
        return {"ok": True}

    @_api
    def kosell_map_delete(self, data: dict) -> dict:
        self._require().kosell_map_delete(data.get("marketplace") or "playerok",
                                          (data.get("lot_id") or "").strip())
        return {"ok": True}

    @_api
    def kosell_rentals(self) -> dict:
        return {"ok": True, "items": self._require().kosell_rental_list()}

    def kosell_rent(self, data: dict) -> dict:
        """Ручная аренда из панели (для теста/разовой выдачи). Воркер делает это автоматически."""
        try:
            product_id = int(data["product_id"])
            hours = max(1, int(data.get("hours") or 24))
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "message": "нужны product_id и часы"}
        currency = data.get("currency") if data.get("currency") in ("RUB", "USD") else "RUB"
        r = self._kosell_net(lambda c: c.rent(product_id, hours, currency), timeout=60)
        if "error" in r:
            return {"ok": False, "message": self._kosell_err(r)}
        rr = r["val"]
        with self._lock:
            if self.storage is not None:
                self.storage.kosell_rental_add(
                    rr["rental_uid"], "playerok", None, None, product_id,
                    rr.get("product_name", ""), rr.get("steam_login", ""), hours,
                    rr.get("started_at", ""), rr.get("expires_at", ""),
                    rr.get("status", "active"), time.time())
                self.storage.log("kosell_rent", data={"uid": rr["rental_uid"],
                                 "product": rr.get("product_name", "")})
        return {"ok": True, **rr}

    def kosell_credentials(self, rental_uid: str) -> dict:
        r = self._kosell_net(lambda c: c.credentials(rental_uid))
        if "error" in r:
            return {"ok": False, "message": self._kosell_err(r)}
        return {"ok": True, **r["val"]}

    def kosell_code(self, rental_uid: str) -> dict:
        r = self._kosell_net(lambda c: c.code(rental_uid))
        if "error" in r:
            return {"ok": False, "message": self._kosell_err(r)}
        return {"ok": True, **r["val"]}

    def kosell_extend(self, data: dict) -> dict:
        uid = data.get("rental_uid")
        try:
            hours = max(1, int(data.get("hours") or 1))
        except (TypeError, ValueError):
            return {"ok": False, "message": "часы — целое число"}
        currency = data.get("currency") if data.get("currency") in ("RUB", "USD") else "RUB"
        r = self._kosell_net(lambda c: c.extend(uid, hours, currency), timeout=60)
        if "error" in r:
            return {"ok": False, "message": self._kosell_err(r)}
        rr = r["val"]
        with self._lock:
            if self.storage is not None:
                self.storage.kosell_rental_update_status(uid, "active", rr.get("new_expires_at"))
        return {"ok": True, **rr}

    def kosell_terminate(self, rental_uid: str) -> dict:
        r = self._kosell_net(lambda c: c.terminate(rental_uid), timeout=60)
        if "error" in r:
            return {"ok": False, "message": self._kosell_err(r)}
        with self._lock:
            if self.storage is not None:
                self.storage.kosell_rental_update_status(rental_uid, "terminated")
                self.storage.log("kosell_terminate", data={"uid": rental_uid})
        return {"ok": True}

    def kosell_logout(self, rental_uid: str) -> dict:
        r = self._kosell_net(lambda c: c.full_logout(rental_uid), timeout=60)
        if "error" in r:
            return {"ok": False, "message": self._kosell_err(r)}
        with self._lock:
            if self.storage is not None:
                self.storage.log("kosell_logout", data={"uid": rental_uid})
        return {"ok": True, "message": "выход со всех устройств выполнен"}

    # ---------- плагин «Гарантия/Автозамена» ----------
    @_api
    def warranty_list(self) -> dict:
        return {"ok": True, "items": self._require().warranty_list()}

    @_api
    def warranty_resolve(self, case_id) -> dict:
        self._require().warranty_resolve(int(case_id))
        return {"ok": True}

    @_api
    def warranty_settings_get(self) -> dict:
        s = self._require()
        return {"ok": True,
                "enabled": s.get_setting("warranty_enabled") != "0",
                "autoretry": int(s.get_setting("warranty_autoretry") or 2)}

    @_api
    def warranty_settings_set(self, data: dict) -> dict:
        s = self._require()
        s.set_setting("warranty_enabled", "1" if data.get("enabled", True) else "0")
        try:
            n = max(0, min(5, int(data.get("autoretry", 2))))
        except (TypeError, ValueError):
            return {"error": "Число повторов — целое 0–5"}
        s.set_setting("warranty_autoretry", str(n))
        return {"ok": True}

    # ---------- плагин «Репрайсер» (калькулятор цены/маржи, без записи цен) ----------
    def kosell_pricing(self) -> dict:
        """Для каждого связанного лота: живая цена KoSell × срок → рекомендованная цена Playerok
        с наценкой и маржа. Только расчёт/совет — цены никуда не пишем (безопасно)."""
        with self._lock:
            maps = [m for m in self._require().kosell_map_list() if m.get("enabled")]
        if not maps:
            return {"ok": True, "items": []}

        def compute(c):
            out = []
            for m in maps:
                try:
                    pr = c.calculate_price(int(m["product_id"]), int(m["hours"]))
                except Exception:  # noqa: BLE001 — один лот не валит весь расчёт
                    pr = {}
                cost = (pr.get("total_rub") if m["currency"] == "RUB" else pr.get("total_usd")) or 0
                mk = float(m["markup_percent"])
                suggested = round(cost * (1 + mk / 100), 2)
                out.append({"lot_id": m["lot_id"], "product_name": m["product_name"],
                            "hours": m["hours"], "currency": m["currency"], "cost": round(cost, 2),
                            "markup": mk, "suggested": suggested, "margin": round(suggested - cost, 2)})
            return out

        r = self._kosell_net(lambda c: compute(c), timeout=60)
        if "error" in r:
            return {"ok": False, "message": self._kosell_err(r)}
        return {"ok": True, "items": r["val"]}

    def repricer_scan(self) -> dict:
        """Рыночный репрайсер: все мои лоты Playerok + статистика похожих (мин/средняя/макс).
        Похожие = та же игра и категория у других продавцов (публичный каталог Playerok).
        Только аналитика — цены не меняет. Сеть идёт ВНЕ self._lock (как pl_check)."""
        with self._lock:
            s = self._require()
            token = s.get_setting("pl_token")
            proxy = s.get_setting("pl_proxy")
        if not token:
            return {"ok": False, "message": "Playerok не подключён — задайте токен в Настройках"}
        from rentbot import repricer
        r = self._run_net(lambda: repricer.scan(token, proxy), timeout=150)
        if r.get("error"):
            return {"ok": False, "message": r["error"]}
        v = r["val"]
        with self._lock:
            if self.storage is not None:
                self.storage.log("repricer_scan", data={"n": v.get("my_count", 0)})
        return {"ok": True, **v}

    # ---------- плагин «Аренда PUBG Mobile» ----------
    _MOBILE_LOGIN_TYPES = ("facebook", "google", "twitter", "vk", "apple", "email")

    @_api
    def mobile_accounts(self) -> dict:
        s = self._require()
        return {"ok": True, "items": s.mobile_list(),
                "autokick": s.get_setting("mobile_autokick") == "1"}

    @_api
    def mobile_set_autokick(self, enabled) -> dict:
        """Тумблер авто-кика по окончании аренды PUBG Mobile (эксперим., по умолчанию выкл)."""
        s = self._require()
        s.set_setting("mobile_autokick", "1" if enabled else "0")
        s.log("mobile_autokick_toggle", data={"on": bool(enabled)})
        return {"ok": True}

    @_api
    def mobile_add(self, data: dict) -> dict:
        s = self._require()
        login = (data.get("login") or "").strip()
        if not login:
            return {"error": "Укажите логин аккаунта"}
        lt = data.get("login_type") if data.get("login_type") in self._MOBILE_LOGIN_TYPES else "facebook"
        tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]
        aid = s.mobile_add(
            game=(data.get("game") or "pubg_mobile").strip(), login_type=lt, login=login,
            password=(data.get("password") or "").strip() or None,
            email=(data.get("email") or "").strip() or None,
            email_password=(data.get("email_password") or "").strip() or None,
            imap_host=(data.get("imap_host") or "").strip() or None,
            proxy=(data.get("proxy") or "").strip() or None,
            note=(data.get("note") or "").strip(), tags=tags)
        s.log("mobile_added", data={"id": aid, "login": login})
        return {"ok": True, "id": aid}

    @_api
    def mobile_update(self, data: dict) -> dict:
        s = self._require()
        aid = int(data.get("id"))
        s.mobile_get(aid)  # KeyError → обёртка вернёт ошибку
        login = (data.get("login") or "").strip()
        if not login:
            return {"error": "Логин не может быть пустым"}
        lt = data.get("login_type") if data.get("login_type") in self._MOBILE_LOGIN_TYPES else "facebook"
        tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]
        s.mobile_update(
            aid, game=(data.get("game") or "pubg_mobile").strip(), login_type=lt, login=login,
            email=(data.get("email") or "").strip() or None,
            imap_host=(data.get("imap_host") or "").strip() or None,
            proxy=(data.get("proxy") or "").strip() or None,
            note=(data.get("note") or "").strip(), tags=tags,
            password=(data.get("password") or "").strip() or None,
            email_password=(data.get("email_password") or "").strip() or None)
        if data.get("state") in ("free", "paused", "rented"):
            s.mobile_set_state(aid, data["state"])
        s.log("mobile_updated", data={"id": aid})
        return {"ok": True}

    @_api
    def mobile_delete(self, account_id) -> dict:
        s = self._require()
        acc = s.mobile_get(int(account_id))
        if acc["state"] == "rented":
            return {"error": "Аккаунт в аренде — нельзя удалить"}
        s.mobile_delete(int(account_id))
        s.log("mobile_deleted", data={"id": int(account_id)})
        return {"ok": True}

    @_api
    def mobile_credentials(self, account_id) -> dict:
        """Показать владельцу доступы мобильного аккаунта (логин/пароль + почта)."""
        s = self._require()
        acc = s.mobile_get(int(account_id))
        s.log("mobile_creds_viewed", data={"id": int(account_id)})
        return {"ok": True, "login_type": acc["login_type"], "login": acc["login"],
                "password": acc["password"], "email": acc["email"],
                "email_password": acc["email_password"]}

    @_api
    def mobile_lots(self) -> dict:
        s = self._require()
        lots = s.mobile_lot_list()
        for lot in lots:
            lot["free"] = s.mobile_free_count(lot["game"])
        games = sorted({a["game"] for a in s.mobile_list()})
        return {"ok": True, "items": lots, "games": games or ["pubg_mobile"]}

    @_api
    def mobile_lot_set(self, data: dict) -> dict:
        s = self._require()
        lot_id = (data.get("lot_id") or "").strip()
        if not lot_id:
            return {"error": "Укажите id лота Playerok"}
        game = (data.get("game") or "pubg_mobile").strip()
        try:
            hours = max(1, int(data.get("hours") or 24))
        except (TypeError, ValueError):
            return {"error": "Часы — целое число"}
        s.mobile_lot_set(data.get("marketplace") or "playerok", lot_id, game, hours,
                         bool(data.get("enabled", True)))
        s.log("mobile_lot_set", data={"lot_id": lot_id, "game": game})
        return {"ok": True}

    @_api
    def mobile_lot_delete(self, data: dict) -> dict:
        self._require().mobile_lot_delete(data.get("marketplace") or "playerok",
                                          (data.get("lot_id") or "").strip())
        return {"ok": True}

    @_api
    def mobile_rentals(self) -> dict:
        return {"ok": True, "items": self._require().mobile_rental_list()}

    def mobile_test_code(self, account_id) -> dict:
        """Проверить чтение кода с почты аккаунта (IMAP) — сеть ВНЕ self._lock."""
        try:
            with self._lock:
                acc = self._require().mobile_get(int(account_id))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"{type(e).__name__}: {e}"}
        if not acc.get("email") or not acc.get("email_password"):
            return {"ok": False, "message": "у аккаунта не задана почта или пароль почты"}

        def run():
            from rentbot import mobile_otp
            code, info = mobile_otp.read_otp(acc["email"], acc["email_password"],
                                             acc.get("imap_host") or None)
            return {"code": code, "info": info}

        r = self._run_net(run, timeout=25)
        if "error" in r:
            return {"ok": False, "message": r["error"]}
        v = r["val"]
        return {"ok": bool(v["code"]), "code": v["code"], "message": v["info"]}

    # ---------- вход в Playerok по email-коду (альтернатива cookie) ----------
    def _run_net(self, fn, timeout: float = 40.0) -> dict:
        """Выполнить сетевую операцию в отдельном потоке БЕЗ удержания self._lock (не морозит UI)."""
        result: dict = {}

        def run():
            try:
                result["val"] = fn()
            except Exception as e:  # noqa: BLE001
                result["err"] = f"{type(e).__name__}: {str(e)[:200]}"

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(timeout=timeout)
        if th.is_alive():
            return {"error": "таймаут сети — проверьте прокси/соединение"}
        if "err" in result:
            return {"error": result["err"]}
        return {"ok": True, "val": result.get("val")}

    def pl_check_connection(self) -> dict:
        """Проверить, что сохранённый токен Playerok рабочий (отвечает профилем продавца).
        Работает одинаково и для cookie, и для email-входа — проверяется итоговый token."""
        with self._lock:
            s = self._require()
            token = s.get_setting("pl_token")
            proxy = s.get_setting("pl_proxy")
            method = s.get_setting("pl_auth_method") or "cookie"
        if not token:
            return {"ok": True, "connected": False,
                    "message": "Не подключено — задайте cookie или войдите по email"}
        from rentbot import playerok_auth
        r = self._run_net(lambda: playerok_auth.check_token(token, proxy))
        if r.get("error"):
            with self._lock:
                if self.storage is not None:
                    self.storage.log("pl_check", data={"ok": False})
            return {"ok": True, "connected": False, "message": r["error"]}
        v = r["val"]
        with self._lock:
            if self.storage is not None:
                self.storage.log("pl_check", data={"ok": True, "user": v.get("username")})
        return {"ok": True, "connected": True, "method": method,
                "username": v.get("username"), "unread": v.get("unreadChatsCounter")}

    def pl_email_request_code(self, email: str) -> dict:
        """Шаг 1: попросить Playerok прислать код на почту."""
        email = (email or "").strip()
        if "@" not in email:
            return {"error": "Введите корректный email"}
        with self._lock:
            proxy = self._require().get_setting("pl_proxy")
        from rentbot import playerok_auth
        auth = playerok_auth.PlayerokAuth(proxy=proxy)

        r = self._run_net(lambda: auth.request_code(email))
        if r.get("error"):
            try:
                auth.close()
            except Exception:
                pass
            return r
        self._pl_auth = auth  # держим сессию до ввода кода
        return {"ok": True, "message": f"Код отправлен на {email}. Проверьте почту."}

    def pl_email_submit_code(self, email: str, code: str) -> dict:
        """Шаг 2: подтвердить код → получить token и сохранить его."""
        email = (email or "").strip()
        code = (code or "").strip()
        if not code:
            return {"error": "Введите код из письма"}
        auth = self._pl_auth
        if auth is None:
            return {"error": "Сначала запросите код"}

        r = self._run_net(lambda: auth.submit_code(email, code))
        if r.get("error"):
            return r
        token, viewer = r["val"]
        with self._lock:
            s = self._require()
            s.set_setting("pl_token", token, secret=True)
            s.set_setting("pl_auth_method", "email")
            s.set_setting("pl_email", email)
            s.log("pl_email_login", data={"user": viewer.get("username")})
        try:
            auth.close()
        except Exception:
            pass
        self._pl_auth = None
        return {"ok": True, "username": viewer.get("username") or ""}

    # ---------- воркер (реальный запуск движка + сервисов в фоновом потоке) ----------
    @_api
    def worker_toggle(self) -> dict:
        self._require()
        from rentbot.worker import ThreadedWorker
        if self._worker is None:
            self._worker = ThreadedWorker(self.db_path, self.storage.vault)
        if self._worker.is_running():
            self._worker.stop()
            return {"ok": True, "worker": False}
        r = self._worker.start()
        if r.get("error"):
            return {"error": r["error"], "worker": False}
        return {"ok": True, "worker": True}

    # ---------- демо-данные для первого знакомства ----------
    @_api
    def seed_demo(self) -> dict:
        """Наполнить БД примерами (только если пусто) — чтобы панель не была голой."""
        s = self._require()
        if s.list_accounts():
            return {"error": "В базе уже есть аккаунты — демо не добавляю"}
        demo = [
            ("cs2", "cs_prime_01", "faceit_verified,prime"),
            ("cs2", "cs_prime_02", "prime"),
            ("dota2", "dota_mmr_01", "2k_mmr"),
            ("faceit", "faceit_lvl10", "faceit_verified"),
        ]
        ss = base64.b64encode(os.urandom(20)).decode()
        for game, login, tags in demo:
            s.add_account(Account(id=None, game=game, login=login, password="demo-pass",
                                  shared_secret=ss, identity_secret=ss,
                                  mafile_json=json.dumps({"shared_secret": ss, "identity_secret": ss}),
                                  tags=[t for t in tags.split(",") if t]))
        s.bind_lot(LotBinding("playerok", "demo-lot-cs2", "cs2", 6.0, RotationMode.AUTO,
                              "CS2 Prime 6ч автовыдача"))
        s.log("demo_seeded")
        return {"ok": True}
