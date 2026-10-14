"""Вход в Playerok по email-коду (нативный passwordless-флоу площадки).

У Playerok нет логина/пароля — только email + одноразовый код:
  1) getEmailAuthCode(email)         → площадка шлёт код на почту;
  2) checkEmailAuthCode(email, code) → возвращает профиль и ставит cookie `token`.

Отсюда бот сам достаёт `token` и дальше работает как с обычной cookie-авторизацией.
Синхронный curl_cffi (панель зовёт из своего потока). Код, как правило, привязан к email,
но одну сессию держим между запросом и подтверждением на случай pre-auth cookie/CSRF.
"""
from __future__ import annotations

from .adapters.playerok import (BASE, GRAPHQL_URL, IMPERSONATE, REQUIRED_HEADERS,
                                 _is_cf_challenge)

Q_VIEWER = "query viewer { viewer { id username unreadChatsCounter } }"

Q_GET_CODE = ("mutation getEmailAuthCode($email: String!) { "
              "getEmailAuthCode(input: {email: $email}) }")
Q_CHECK_CODE = ("mutation checkEmailAuthCode($input: CheckEmailAuthCodeInput!) { "
                "checkEmailAuthCode(input: $input) { id username } }")


def check_token(token: str, proxy: str | None = None) -> dict:
    """Проверить сохранённый токен (cookie или полученный по email): вернуть профиль продавца
    {id, username, unreadChatsCounter} или бросить исключение с причиной. Сеть, read-only."""
    from curl_cffi import requests
    s = (requests.Session(impersonate=IMPERSONATE, proxy=proxy) if proxy
         else requests.Session(impersonate=IMPERSONATE))
    try:
        s.cookies.set("token", token, domain="playerok.com")
        r0 = s.get(BASE + "/")
        if _is_cf_challenge(r0.status_code, r0.text):
            raise RuntimeError("Cloudflare не пропустил (попробуйте прокси)")
        r = s.post(GRAPHQL_URL, headers=REQUIRED_HEADERS,
                   json={"operationName": "viewer", "query": Q_VIEWER, "variables": {}})
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"HTTP {r.status_code}, тело не JSON")
        if data.get("errors"):
            raise RuntimeError(data["errors"][0].get("message", "ошибка GraphQL"))
        viewer = (data.get("data") or {}).get("viewer")
        if not viewer:
            raise RuntimeError("токен невалиден или протух")
        return viewer
    finally:
        try:
            s.close()
        except Exception:
            pass


class PlayerokAuth:
    def __init__(self, proxy: str | None = None):
        from curl_cffi import requests
        self.s = (requests.Session(impersonate=IMPERSONATE, proxy=proxy) if proxy
                  else requests.Session(impersonate=IMPERSONATE))

    def _gql(self, op: str, query: str, variables: dict) -> dict:
        r = self.s.post(GRAPHQL_URL, headers=REQUIRED_HEADERS,
                        json={"operationName": op, "query": query, "variables": variables})
        if _is_cf_challenge(r.status_code, r.text):
            raise RuntimeError("Cloudflare не пропустил (попробуйте прокси)")
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"{op}: HTTP {r.status_code}, тело не JSON")
        if data.get("errors"):
            msg = data["errors"][0].get("message", str(data["errors"]))
            raise RuntimeError(msg)
        return data["data"]

    def warmup(self) -> None:
        r = self.s.get(BASE + "/")
        if _is_cf_challenge(r.status_code, r.text):
            raise RuntimeError("Cloudflare challenge на входе (попробуйте прокси)")

    def request_code(self, email: str) -> None:
        """Шаг 1: попросить площадку прислать код на почту."""
        self.warmup()
        self._gql("getEmailAuthCode", Q_GET_CODE, {"email": email})

    def submit_code(self, email: str, code: str) -> tuple[str, dict]:
        """Шаг 2: подтвердить код. Вернуть (token, viewer)."""
        data = self._gql("checkEmailAuthCode", Q_CHECK_CODE,
                         {"input": {"email": email, "code": code}})
        viewer = data.get("checkEmailAuthCode") or {}
        token = self._extract_token()
        if not token:
            raise RuntimeError("код принят, но cookie token не получена — сообщите разработчику")
        return token, viewer

    def _extract_token(self) -> str | None:
        try:
            tok = self.s.cookies.get("token")
            if tok:
                return tok
        except Exception:
            pass
        # запасной путь: пройтись по всем cookie
        try:
            for c in self.s.cookies.jar:
                if c.name == "token":
                    return c.value
        except Exception:
            pass
        return None

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:
            pass
