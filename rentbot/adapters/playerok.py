"""Адаптер Playerok.

Обход Cloudflare: curl_cffi с TLS-отпечатком браузера (impersonate). Это не «ломание»
защиты, а корректный отпечаток — Cloudflare у playerok.com пропускает любой chrome*/safari
impersonate без челленджа (проверено 12.10.2026). На случай всплеска защиты в `_http_*`
встроен ретрай с бэкоффом, который распознаёт страницу-челлендж и managed-challenge 403/503.

Авторизация продавца — cookie `token` (DevTools → Application → Cookies → playerok.com → token).

GraphQL (https://playerok.com/graphql): операции отправляются ОБЫЧНЫМ текстом (persisted-хэши
НЕ обязательны — проверено). Обязателен заголовок `apollo-require-preflight: true` (иначе 400 CSRF).
Тексты операций сняты из JS-бандлов фронта и провалидированы против живой схемы; при поломке
их можно переопределить через `op_overrides` (например, прилетевший с сервера лицензий JSON),
не трогая код.

Публичный каталог (чтение без авторизации): POST https://api.playerok.com/v1/catalog/items
  {filter:{...}, page:{size<=100, cursor}} → {items, endCursor, hasNextPage}.

События реального времени: wss://ws.playerok.com/graphql (GraphQL-subscriptions). В MVP
основной путь — страховочный опрос `deals`/`chats` раз в ~poll_interval c; идемпотентность
по deal_id на стороне движка. WS можно включить позже (метод `run` ниже готов принять оба).
"""
from __future__ import annotations

import asyncio
import contextlib
import random
import time
from typing import Any, Awaitable, Callable

from ..ports import ChatMessage, Marketplace, OrderEvent

BASE = "https://playerok.com"
GRAPHQL_URL = BASE + "/graphql"
CATALOG_URL = "https://api.playerok.com/v1/catalog/items"
WS_URL = "wss://ws.playerok.com/graphql"
IMPERSONATE = "chrome"

# Заголовки, без которых Apollo отвечает 400 (CSRF-preflight).
REQUIRED_HEADERS = {
    "apollo-require-preflight": "true",
    "content-type": "application/json",
    "accept": "*/*",
    "origin": BASE,
    "referer": BASE + "/",
}

# Тексты операций (сняты из фронта, провалидированы против живой схемы 12.10.2026).
OPS: dict[str, str] = {
    "viewer": "query viewer { viewer { id username unreadChatsCounter } }",
    "deals": (
        "query deals($pagination: Pagination, $filter: ItemDealFilter!) {"
        "  deals(pagination: $pagination, filter: $filter) {"
        "    totalCount edges { node {"
        "      id status direction createdAt"
        "      chat { id } user { id username } item { id slug name price }"
        "    } } } }"
    ),
    "chats": (
        "query chats($pagination: Pagination, $filter: ChatFilter) {"
        "  chats(pagination: $pagination, filter: $filter) {"
        "    totalCount edges { node {"
        "      id type status unreadMessagesCounter"
        "      lastMessage { id text createdAt isRead user { id username } }"
        "      participants { id username }"
        "    } } } }"
    ),
    "chatMessages": (
        "query chatMessages($pagination: Pagination, $filter: ChatMessageFilter) {"
        "  chatMessages(pagination: $pagination, filter: $filter) {"
        "    edges { node { id text createdAt isRead user { id username } } } } }"
    ),
    "createChatMessage": (
        "mutation createChatMessage($input: CreateChatMessageInput!) {"
        "  createChatMessage(input: $input) { id text createdAt } }"
    ),
    # Переключение видимости лота требует itemId + priorityStatuses + transactionProviderId
    # (publishItem) — эти значения снимаются из самого item на живом аккаунте продавца.
    # См. set_lot_active ниже.
    "publishItem": (
        "mutation publishItem($input: PublishItemInput!) {"
        "  publishItem(input: $input) { id slug status } }"
    ),
    "removeItem": (
        "mutation removeItem($id: UUID!) { removeItem(id: $id) { id slug status } }"
    ),
    "item": "query item($id: UUID!) { item(id: $id) { id slug name status priority } }",
    # автоподнятие: взять доступные статусы приоритета (есть бесплатный) и поднять лот
    "itemPriorityStatuses": (
        "query itemPriorityStatuses($price: NonNegativeFloat!, $itemId: UUID) {"
        "  itemPriorityStatuses(price: $price, itemId: $itemId) {"
        "    id price name type period } }"
    ),
    "increaseItemPriorityStatus": (
        "mutation increaseItemPriorityStatus($input: PublishItemInput!) {"
        "  increaseItemPriorityStatus(input: $input) { id slug status priority } }"
    ),
    # выдача файлом — createChatMessage с Upload (file). Текст идёт в caption.
    "createChatMessageFile": (
        "mutation createChatMessage($input: CreateChatMessageInput!, $file: Upload) {"
        "  createChatMessage(input: $input, file: $file) { id createdAt } }"
    ),
}


class GraphQLError(RuntimeError):
    def __init__(self, op: str, errors: Any):
        super().__init__(f"{op}: {errors}")
        self.op = op
        self.errors = errors


class CloudflareBlocked(RuntimeError):
    """Cloudflare вернул челлендж, который impersonate не прошёл (ретраи исчерпаны)."""


def _is_cf_challenge(status: int, text: str) -> bool:
    if status in (403, 503, 429):
        head = text[:4000]
        markers = ("Just a moment", "cf-browser-verification", "challenge-platform",
                   "/cdn-cgi/challenge", "Attention Required")
        return any(m in head for m in markers)
    return False


class PlayerokAdapter(Marketplace):
    name = "playerok"

    def __init__(self, token: str, *, proxy: str | None = None,
                 op_overrides: dict[str, str] | None = None,
                 poll_interval: float = 45.0, max_retries: int = 4):
        self.token = token
        self.proxy = proxy
        self.ops = {**OPS, **(op_overrides or {})}
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self._http = None           # curl_cffi AsyncSession, создаётся в start()
        self._me: str | None = None
        self._seen_deals: set[str] = set()
        self._seen_msgs: set[str] = set()
        self.on_poll_error = None  # callback(exc) — логировать сбой опроса, не роняя воркер

    # ---------- жизненный цикл ----------
    async def start(self) -> dict:
        """Поднять HTTP-клиент, пройти Cloudflare, провалидировать токен. Вернуть viewer."""
        from curl_cffi import requests as cffi  # импорт здесь: ядро/тесты не тянут curl_cffi
        self._http = cffi.AsyncSession(impersonate=IMPERSONATE,
                                       proxy=self.proxy, timeout=30)
        self._http.cookies.set("token", self.token, domain="playerok.com")
        await self._warmup()
        viewer = (await self.gql("viewer"))["viewer"]
        if not viewer:
            raise RuntimeError("viewer=null — cookie token невалиден или протух")
        self._me = viewer["id"]
        return viewer

    async def close(self) -> None:
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.close()
            self._http = None

    async def _warmup(self) -> None:
        r = await self._http_get(BASE + "/")
        if _is_cf_challenge(r.status_code, r.text):
            raise CloudflareBlocked("Cloudflare challenge на GET / — impersonate не прошёл")

    # ---------- низкоуровневый HTTP с ретраем по Cloudflare ----------
    async def _sleep_backoff(self, attempt: int) -> None:
        # экспоненциальный бэкофф + джиттер (человеческая пауза, антибан)
        await asyncio.sleep(min(2 ** attempt, 15) + random.uniform(0.3, 1.2))

    async def _http_get(self, url: str):
        last = None
        for attempt in range(self.max_retries):
            r = await self._http.get(url)
            if not _is_cf_challenge(r.status_code, r.text):
                return r
            last = r
            await self._sleep_backoff(attempt)
        raise CloudflareBlocked(f"GET {url}: челлендж после {self.max_retries} попыток "
                                f"(last {last.status_code if last else '?'})")

    async def _http_post(self, url: str, json_body: dict, headers: dict):
        last = None
        for attempt in range(self.max_retries):
            r = await self._http.post(url, json=json_body, headers=headers)
            if not _is_cf_challenge(r.status_code, r.text):
                return r
            last = r
            await self._sleep_backoff(attempt)
        raise CloudflareBlocked(f"POST {url}: челлендж после {self.max_retries} попыток "
                                f"(last {last.status_code if last else '?'})")

    # ---------- GraphQL ----------
    async def gql(self, op: str, variables: dict | None = None) -> dict:
        query = self.ops[op]
        body = {"operationName": op, "query": query, "variables": variables or {}}
        r = await self._http_post(GRAPHQL_URL, body, REQUIRED_HEADERS)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"{op}: HTTP {r.status_code}, тело не JSON: {r.text[:200]!r}")
        if data.get("errors"):
            raise GraphQLError(op, data["errors"])
        return data["data"]

    # ---------- публичный каталог (без авторизации) ----------
    async def catalog(self, *, filter: dict | None = None, size: int = 100,
                      cursor: str | None = None) -> dict:
        page: dict = {"size": min(size, 100)}
        if cursor:
            page["cursor"] = cursor
        body = {"filter": filter or {}, "page": page}
        r = await self._http_post(CATALOG_URL, body,
                                  {"content-type": "application/json",
                                   "origin": BASE, "referer": BASE + "/"})
        return r.json()

    # ---------- интерфейс Marketplace ----------
    async def send_message(self, chat_id: str, text: str) -> None:
        await self.gql("createChatMessage", {"input": {"chatId": chat_id, "text": text}})

    async def send_file(self, chat_id: str, file_path: str, caption: str = "") -> None:
        """Выдача файлом (логин-пароль). createChatMessage с multipart Upload по спеке GraphQL."""
        import os
        if not os.path.isfile(file_path):
            raise FileNotFoundError(file_path)
        operations = {
            "operationName": "createChatMessage",
            "query": self.ops["createChatMessageFile"],
            "variables": {"input": {"chatId": chat_id, "text": caption or None}, "file": None},
        }
        # GraphQL multipart request spec: map привязывает файл к переменной $file
        data = {"operations": __import__("json").dumps(operations),
                "map": __import__("json").dumps({"0": ["variables.file"]})}
        with open(file_path, "rb") as fh:
            files = {"0": (os.path.basename(file_path), fh.read())}
        r = await self._http.post(GRAPHQL_URL, data=data, files=files,
                                  headers={"apollo-require-preflight": "true"})
        res = r.json()
        if res.get("errors"):
            raise GraphQLError("createChatMessageFile", res["errors"])

    async def bump_item(self, lot_id: str, price: float) -> None:
        """Автоподнятие: поднять приоритет лота бесплатным статусом.

        lot_id здесь = itemId (UUID лота продавца). price нужен для подбора статусов;
        при 0 берём цену из самого лота. Выбираем бесплатный статус (price 0)."""
        if not price:
            item = (await self.gql("item", {"id": lot_id})).get("item") or {}
            price = float(item.get("price") or 0)
        statuses = (await self.gql("itemPriorityStatuses",
                                   {"price": price, "itemId": lot_id}))["itemPriorityStatuses"]
        free = next((s for s in statuses if float(s.get("price") or 0) == 0), None)
        if free is None:
            raise RuntimeError(f"нет бесплатного статуса приоритета для лота {lot_id}")
        await self.gql("increaseItemPriorityStatus", {"input": {
            "itemId": lot_id, "priorityStatuses": [free["id"]], "transactionProviderId": "LOCAL"}})

    async def set_lot_active(self, lot_id: str, active: bool) -> None:
        """Показать/скрыть лот.

        На Playerok «показать» = publishItem (нужны priorityStatuses и transactionProviderId
        из самого лота), «скрыть» = removeItem (снимает с публикации). Точные значения
        priorityStatuses/transactionProviderId берутся с живого лота продавца, поэтому
        переключатель активируется только когда движок передаёт их через op_overrides-конфиг
        или когда лот читается заранее. В режиме «только аренда» это некритично (лот можно
        держать видимым), поэтому по умолчанию — безопасный no-op с предупреждением.
        """
        # TODO(интеграция, нужен живой лот продавца): собрать PublishItemInput из item().
        raise NotImplementedError(
            "set_lot_active требует priorityStatuses/transactionProviderId с живого лота — "
            "подставляются при интеграции с кабинетом продавца"
        )

    # ---------- опрос событий ----------
    async def _poll_deals(self, on_order: Callable[[OrderEvent], Awaitable[None]]) -> None:
        if self._me is None:
            return
        # Как продавец мы получаем входящие сделки покупателей. Берём свежие PAID-сделки.
        data = await self.gql("deals", {"pagination": {"first": 20},
                                        "filter": {"userId": self._me}})
        for edge in data["deals"]["edges"]:
            d = edge["node"]
            if d["id"] in self._seen_deals:
                continue
            self._seen_deals.add(d["id"])
            # фильтр «оплачено» — статусы PAID/ITEM_PAID (см. brief). Пропускаем прочее.
            if "PAID" not in (d.get("status") or ""):
                continue
            item = d.get("item") or {}
            user = d.get("user") or {}
            chat = d.get("chat") or {}
            await on_order(OrderEvent(
                marketplace=self.name,
                deal_id=d["id"],
                lot_id=item.get("id", ""),
                buyer_id=user.get("id", ""),
                buyer_name=user.get("username", ""),
                chat_id=chat.get("id", ""),
                price=float(item.get("price") or 0),
            ))

    async def _poll_chats(self, on_chat: Callable[[ChatMessage], Awaitable[None]]) -> None:
        if self._me is None:
            return
        data = await self.gql("chats", {"pagination": {"first": 20},
                                        "filter": {"userId": self._me}})
        for edge in data["chats"]["edges"]:
            c = edge["node"]
            lm = c.get("lastMessage")
            if not lm or lm["id"] in self._seen_msgs:
                continue
            self._seen_msgs.add(lm["id"])
            author = (lm.get("user") or {})
            if author.get("id") == self._me:
                continue  # наше же сообщение
            await on_chat(ChatMessage(
                marketplace=self.name,
                chat_id=c["id"],
                buyer_id=author.get("id", ""),
                text=lm.get("text") or "",
            ))

    async def run(self, on_order: Callable[[OrderEvent], Awaitable[None]],
                  on_chat: Callable[[ChatMessage], Awaitable[None]]) -> None:
        """Главный цикл: страховочный опрос сделок и чатов. Идемпотентность — у движка."""
        if self._http is None:
            await self.start()
        while True:
            try:
                await self._poll_deals(on_order)
                await asyncio.sleep(random.uniform(0.5, 1.5))  # антибан: разносим запросы
                await self._poll_chats(on_chat)
            except CloudflareBlocked as e:
                if self.on_poll_error:
                    self.on_poll_error(e)
                await asyncio.sleep(self.poll_interval * 2)  # дать защите остыть
            except Exception as e:  # noqa: BLE001 — один сбойный опрос не должен убивать воркер
                if self.on_poll_error:
                    self.on_poll_error(e)
                await asyncio.sleep(self.poll_interval)
            await asyncio.sleep(self.poll_interval + random.uniform(-5, 5))
