"""Источник общего чёрного списка арендаторов (единого для всех продавцов).

По архитектуре (DB.md) общий ЧС живёт на нашем сервере лицензий (FastAPI + Postgres),
который делается на этапе платного запуска. Здесь — клиент: тянет список по HTTP с этого
сервера. Пока сервер не поднят и URL не задан — возвращается небольшой ДЕМО-набор, чтобы
функция была проверяема (buyer_id явно синтетические).

Формат записи: {"marketplace": "playerok", "buyer_id": "<id>", "reason": "<текст>"}.
"""
from __future__ import annotations

# Демо-набор (пока нет сервера). buyer_id намеренно нереальные — это тестовые данные.
_DEMO = [
    {"marketplace": "playerok", "buyer_id": "demo_scam_001", "reason": "не вернул арендованный аккаунт (demo)"},
    {"marketplace": "playerok", "buyer_id": "demo_scam_002", "reason": "чарджбэк после аренды (demo)"},
    {"marketplace": "playerok", "buyer_id": "demo_scam_003", "reason": "сменил пароль, угнал аккаунт (demo)"},
]


def fetch_global_blacklist(url: str | None = None, token: str | None = None) -> tuple[list[dict], str]:
    """Вернуть (записи, источник). Если URL не задан — демо-набор.
    При заданном URL ходит на сервер лицензий (curl_cffi), ожидая JSON-массив записей."""
    if not url:
        return list(_DEMO), "demo"
    from curl_cffi import requests
    headers = {"authorization": f"Bearer {token}"} if token else {}
    r = requests.get(url, headers=headers, timeout=20)
    data = r.json()
    entries = data.get("items", data) if isinstance(data, dict) else data
    out = [{"marketplace": e.get("marketplace", "playerok"),
            "buyer_id": str(e.get("buyer_id", "")), "reason": e.get("reason", "")}
           for e in entries if e.get("buyer_id")]
    return out, "server"
