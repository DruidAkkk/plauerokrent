"""Адаптер KoSell — почасовая аренда Steam-аккаунтов как ПОСТАВЩИК (дропшиппинг на Playerok).

KoSell держит аккаунты, выдаёт Steam Guard-коды, умеет сменить пароль и выйти со всех устройств
на СВОЕЙ стороне — мы только перепродаём аренду на Playerok. Поэтому на нашей стороне НЕТ ни
maFile, ни логина в Steam: мы оркеструем заказы через их REST API.

Авторизация — заголовок `X-API-Key` (секрет из сейфа приложения). Ключ НИКОГДА не логируем и не
коммитим. Лимит у них: 300 запросов / 60 c на ключ.

Доки: https://kosell.store/developers  ·  локальная копия OpenAPI: docs/kosell_openapi.json
"""
from __future__ import annotations

from typing import Any

from curl_cffi import requests as crequests

BASE = "https://www.kosell.store/api/v1"
TIMEOUT = 30

# Коды ошибок из их документации (HTTP → смысл) для понятных сообщений в панель.
ERROR_RU = {
    "invalid_api_key": "неверный или отсутствующий API-ключ",
    "account_disabled": "аккаунт KoSell отключён",
    "expired": "аренда истекла или не активна",
    "not_active": "аренда истекла или не активна",
    "insufficient_balance": "недостаточно баланса на KoSell для операции",
    "no_stock": "нет свободных аккаунтов по этой игре",
    "not_found": "товар или аренда не найдены",
    "cooldown": "операция на кулдауне или уже выполняется",
    "operation_inflight": "операция уже выполняется",
    "rate_limited": "слишком много запросов — притормозите",
}


class KoSellError(Exception):
    """Ошибка KoSell API. code — машинный код из их ответа (invalid_api_key, no_stock, ...)."""

    def __init__(self, status: int, code: str | None, message: str):
        self.status = status
        self.code = code or ""
        self.message = message
        super().__init__(f"[{status}] {code or 'error'}: {message}")

    @property
    def ru(self) -> str:
        return ERROR_RU.get(self.code, self.message or self.code or f"ошибка {self.status}")


class KoSellClient:
    """Синхронный клиент KoSell. Короткоживущий: создавайте на операцию, ключ держите в сейфе."""

    def __init__(self, api_key: str, proxy: str | None = None, timeout: int = TIMEOUT):
        if not api_key:
            raise KoSellError(401, "invalid_api_key", "не задан API-ключ KoSell")
        self.api_key = api_key
        self.timeout = timeout
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    # ---- низкий уровень ----
    def _req(self, method: str, path: str, body: dict | None = None) -> Any:
        url = BASE + path
        headers = {"X-API-Key": self.api_key, "accept": "application/json"}
        if body is not None:
            headers["content-type"] = "application/json"
        try:
            r = crequests.request(method, url, headers=headers, json=body,
                                  timeout=self.timeout, proxies=self.proxies)
        except Exception as e:  # noqa: BLE001 — сеть/прокси
            raise KoSellError(0, "network", f"сеть/прокси: {type(e).__name__}: {str(e)[:160]}")

        # разбор тела
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = None

        if r.status_code >= 400:
            code, msg = self._extract_error(data, r.text)
            raise KoSellError(r.status_code, code, msg)
        return data

    @staticmethod
    def _extract_error(data: Any, text: str) -> tuple[str | None, str]:
        """Достать (code, message) из ответа FastAPI: обычно {"detail": ...} или {"error","message"}."""
        if isinstance(data, dict):
            det = data.get("detail")
            if isinstance(det, dict):
                return det.get("error") or det.get("code"), det.get("message") or det.get("error") or str(det)
            if isinstance(det, str):
                return det, det
            if data.get("error") or data.get("message"):
                return data.get("error") or data.get("code"), data.get("message") or data.get("error") or ""
        return None, (text or "")[:200]

    # ---- эндпоинты ----
    def balance(self) -> dict:
        """-> {balance_usd, balance_rub, username, role}"""
        return self._req("GET", "/account/balance")

    def products(self) -> list[dict]:
        """Каталог игр: [{id, name, header_image, price_per_hour, price_per_hour_rub,
        available_accounts, min_hours, max_hours, pricing_mode, ...}]"""
        return self._req("GET", "/rental/products")

    def calculate_price(self, product_id: int, hours: int) -> dict:
        """-> {total_usd, total_rub, price_per_hour, discount_percent, ...}"""
        return self._req("POST", "/rental/calculate-price",
                         {"product_id": int(product_id), "hours": int(hours)})

    def calculate_extend_price(self, rental_uid: str, hours: int) -> dict:
        return self._req("POST", "/rental/calculate-extend-price",
                         {"rental_uid": rental_uid, "hours": int(hours)})

    def rent(self, product_id: int, duration_hours: int, currency: str = "RUB",
             idempotency_key: str | None = None) -> dict:
        """Взять аренду. -> {rental_uid, steam_login, expires_at, status, time_remaining_seconds, ...}
        idempotency_key — повторный вызов с тем же ключом не создаёт новую аренду (ставим id заказа)."""
        body: dict = {"product_id": int(product_id), "duration_hours": int(duration_hours),
                      "currency": currency}
        if idempotency_key:
            body["idempotency_key"] = str(idempotency_key)
        return self._req("POST", "/rental/rent", body)

    def active(self) -> list[dict]:
        """Активные аренды на аккаунте KoSell (та же форма, что у rent)."""
        return self._req("GET", "/rental/active")

    def credentials(self, rental_uid: str) -> dict:
        """-> {steam_login, steam_password}. СЕКРЕТ: не логировать пароль."""
        return self._req("GET", f"/rental/{rental_uid}/credentials")

    def code(self, rental_uid: str) -> dict:
        """Steam Guard-код: -> {success, code, expires_in, error}."""
        return self._req("GET", f"/rental/{rental_uid}/code")

    def extend(self, rental_uid: str, hours: int, currency: str = "RUB") -> dict:
        """-> {success, rental_uid, new_expires_at, hours_added, price_paid, currency}"""
        return self._req("POST", f"/rental/{rental_uid}/extend",
                         {"hours": int(hours), "currency": currency})

    def terminate(self, rental_uid: str) -> dict:
        """Завершить аренду досрочно."""
        return self._req("POST", f"/rental/{rental_uid}/terminate")

    def full_logout(self, rental_uid: str) -> dict:
        """Выход аккаунта со всех устройств (на стороне KoSell)."""
        return self._req("POST", f"/rental/{rental_uid}/full-logout")

    def full_logout_status(self, rental_uid: str) -> dict:
        return self._req("GET", f"/rental/{rental_uid}/full-logout/status")

    def rotate_password(self, rental_uid: str) -> dict:
        """Сменить пароль аккаунта (на стороне KoSell)."""
        return self._req("POST", f"/rental/{rental_uid}/rotate-password")

    def rotate_password_status(self, rental_uid: str) -> dict:
        return self._req("GET", f"/rental/{rental_uid}/rotate-password/status")

    # ---- удобные обёртки ----
    def check(self) -> tuple[bool, str]:
        """Проверка подключения: баланс отвечает → ключ рабочий."""
        try:
            b = self.balance()
        except KoSellError as e:
            return False, e.ru
        return True, (f"KoSell: {b.get('username','?')} · "
                      f"баланс {b.get('balance_rub', 0):.2f}₽ / ${b.get('balance_usd', 0):.2f}")
