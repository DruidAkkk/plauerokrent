"""Рыночный репрайсер: статистика по ВСЕМ лотам продавца на Playerok.

Читает все видимые лоты продавца (публичный каталог, фильтр sellerIds) и для каждого
считает статистику похожих лотов — той же игры и категории: минимальная цена, средняя,
максимум и сколько конкурентов дешевле. Только чтение — цены никуда не пишем (безопасно).

Похожие = та же игра + та же категория Playerok (именно так площадка группирует сравнимые
лоты). Синхронный curl_cffi; публичный каталог читается без авторизации, токен нужен только
чтобы узнать свой sellerId (viewer) и вытащить свои лоты.
"""
from __future__ import annotations

from statistics import mean

from .adapters.playerok import (BASE, CATALOG_URL, GRAPHQL_URL, IMPERSONATE,
                                 REQUIRED_HEADERS, _is_cf_challenge)

Q_VIEWER = "query viewer { viewer { id username } }"
_CATALOG_HEADERS = {"content-type": "application/json", "origin": BASE, "referer": BASE + "/"}
_MAX_SELLER_PAGES = 20      # до ~2000 своих лотов
_MAX_SIMILAR_PAGES = 3      # до ~300 конкурентов на категорию — хватает для мин/средней


def _session(proxy: str | None):
    from curl_cffi import requests
    return (requests.Session(impersonate=IMPERSONATE, proxy=proxy) if proxy
            else requests.Session(impersonate=IMPERSONATE))


def _catalog_page(s, filt: dict, cursor: str | None = None, size: int = 100) -> dict:
    page: dict = {"size": size}
    if cursor:
        page["cursor"] = cursor
    r = s.post(CATALOG_URL, headers=_CATALOG_HEADERS, json={"filter": filt, "page": page})
    if _is_cf_challenge(r.status_code, r.text):
        raise RuntimeError("Cloudflare не пропустил каталог (попробуйте прокси)")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"каталог: HTTP {r.status_code}, тело не JSON")


def _catalog_all(s, filt: dict, max_pages: int) -> list[dict]:
    items: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        d = _catalog_page(s, filt, cursor)
        items.extend(d.get("items") or [])
        if not d.get("hasNextPage"):
            break
        cursor = d.get("endCursor")
        if not cursor:
            break
    return items


def scan(token: str, proxy: str | None = None) -> dict:
    """Собрать мои лоты и статистику похожих. Вернуть {username, my_count, items:[...]}.
    Каждый item: {lot_id, slug, name, price, game, category, similar_count, min, avg, max,
    cheaper_than_me, is_lowest}. Бросает RuntimeError при сетевых/авторизационных проблемах."""
    s = _session(proxy)
    try:
        s.cookies.set("token", token, domain="playerok.com")
        r0 = s.get(BASE + "/")
        if _is_cf_challenge(r0.status_code, r0.text):
            raise RuntimeError("Cloudflare не пропустил (попробуйте прокси)")
        vr = s.post(GRAPHQL_URL, headers=REQUIRED_HEADERS,
                    json={"operationName": "viewer", "query": Q_VIEWER, "variables": {}})
        try:
            vd = vr.json()
        except ValueError:
            raise RuntimeError(f"viewer: HTTP {vr.status_code}, тело не JSON")
        if vd.get("errors"):
            raise RuntimeError(vd["errors"][0].get("message", "ошибка viewer"))
        viewer = (vd.get("data") or {}).get("viewer") or {}
        my_id = viewer.get("id")
        if not my_id:
            raise RuntimeError("токен невалиден или протух")

        my_lots = _catalog_all(s, {"sellerIds": [my_id]}, _MAX_SELLER_PAGES)

        # кэш цен похожих по (game_id, category_id) — одна категория запрашивается один раз
        sim_cache: dict[tuple, list[float]] = {}

        def similar_prices(game_id, cat_id) -> list[float]:
            key = (game_id, cat_id)
            if key in sim_cache:
                return sim_cache[key]
            filt: dict = {}
            if game_id:
                filt["gameIds"] = [game_id]
            if cat_id:
                filt["gameCategoryIds"] = [cat_id]
            prices: list[float] = []
            if filt:
                for it in _catalog_all(s, filt, _MAX_SIMILAR_PAGES):
                    if (it.get("seller") or {}).get("id") == my_id:
                        continue  # свои лоты в статистику конкурентов не включаем
                    p = it.get("price")
                    if isinstance(p, (int, float)) and p > 0:
                        prices.append(float(p))
            sim_cache[key] = prices
            return prices

        items: list[dict] = []
        for lot in my_lots:
            game = lot.get("game") or {}
            cat = lot.get("category") or {}
            prices = similar_prices(game.get("id"), cat.get("id"))
            my_price = float(lot.get("price") or 0)
            stat = {
                "lot_id": lot.get("id"),
                "slug": lot.get("slug"),
                "name": lot.get("name"),
                "price": round(my_price, 2),
                "game": game.get("name"),
                "category": cat.get("name"),
                "similar_count": len(prices),
                "min": round(min(prices), 2) if prices else None,
                "avg": round(mean(prices), 2) if prices else None,
                "max": round(max(prices), 2) if prices else None,
                "cheaper_than_me": (sum(1 for p in prices if p < my_price)
                                    if prices and my_price > 0 else None),
                "is_lowest": (my_price <= min(prices)) if prices and my_price > 0 else None,
            }
            items.append(stat)

        return {"username": viewer.get("username"), "my_count": len(my_lots), "items": items}
    finally:
        try:
            s.close()
        except Exception:
            pass
