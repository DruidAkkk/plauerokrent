"""Экспорт продаж в CSV (все виды аренды: Steam / KoSell / PUBG Mobile).

Чистая логика поверх Storage, без сети и без pywebview — поэтому легко тестируется.
Сам файл сохраняет уже backend (нативный диалог «Сохранить»). CSV делаем «дружелюбным
к Excel»: BOM в начале (кириллица не ломается) и разделитель «;» (русская локаль Excel).
"""
from __future__ import annotations

import time

# Разделитель и перевод строки под русский Excel; BOM — чтобы кириллица открывалась корректно.
DELIM = ";"
NEWLINE = "\r\n"
BOM = "﻿"
HEADER = ["Дата", "Плагин", "Товар", "Заказ", "Покупатель", "Цена", "Статус"]

_STATUS_RU = {
    "active": "активна", "ended": "завершена", "expired": "истекла",
    "queued": "в очереди", "resting": "отдых", "cancelled": "отменена",
}


def _fmt_dt(epoch: float | None) -> str:
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def _status_ru(st: str | None) -> str:
    return _STATUS_RU.get((st or "").lower(), st or "")


def sales_rows(storage) -> list[dict]:
    """Единый список продаж по всем плагинам, свежие сверху.

    Steam-аренда несёт покупателя и цену (цена заказа Playerok), KoSell/PUBG — товар и статус
    (цена сделки у них не хранится на нашей стороне, поэтому поля покупателя/цены пустые)."""
    rows: list[dict] = []

    # Steam-аренда: исключаем служебные CANCELLED-маркеры (продление/блок ЧС — не продажи).
    for r in storage.db.execute(
        "SELECT r.started_at AS ts, r.buyer_name AS buyer, r.price AS price, r.state AS state, "
        "r.deal_id AS deal, r.lot_id AS lot, a.game AS game "
        "FROM rentals r LEFT JOIN accounts a ON a.id = r.account_id "
        "WHERE r.state != 'cancelled'"
    ):
        rows.append({"ts": r["ts"], "plugin": "Steam-аренда",
                     "product": r["game"] or r["lot"] or "", "deal": r["deal"] or "",
                     "buyer": r["buyer"] or "", "price": r["price"], "status": r["state"]})

    # KoSell-реселл.
    for r in storage.kosell_rental_list(limit=100000):
        rows.append({"ts": r.get("created"), "plugin": "KoSell",
                     "product": r.get("product_name") or "", "deal": r.get("deal_id") or "",
                     "buyer": "", "price": None, "status": r.get("status")})

    # PUBG Mobile.
    for r in storage.mobile_rental_list(limit=100000):
        rows.append({"ts": r.get("started"), "plugin": "PUBG Mobile",
                     "product": r.get("game") or "", "deal": r.get("deal_id") or "",
                     "buyer": "", "price": None, "status": r.get("status")})

    rows.sort(key=lambda x: (x["ts"] or 0), reverse=True)
    return rows


def _cell(value) -> str:
    """Экранирование значения по RFC 4180 (кавычки/разделитель/перевод строки)."""
    s = "" if value is None else str(value)
    if any(ch in s for ch in (DELIM, '"', "\n", "\r")):
        s = '"' + s.replace('"', '""') + '"'
    return s


def _price(value) -> str:
    if value in (None, ""):
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else f"{f:.2f}"


def sales_csv(storage) -> str:
    """Готовый CSV-текст (с BOM) для сохранения в файл."""
    lines = [DELIM.join(HEADER)]
    for r in sales_rows(storage):
        lines.append(DELIM.join([
            _cell(_fmt_dt(r["ts"])), _cell(r["plugin"]), _cell(r["product"]),
            _cell(r["deal"]), _cell(r["buyer"]), _cell(_price(r["price"])),
            _cell(_status_ru(r["status"])),
        ]))
    return BOM + NEWLINE.join(lines) + NEWLINE
