"""Спайк Playerok: Cloudflare → авторизация по cookie token → чаты/сделки → тестовое сообщение.

Гоняет РЕАЛЬНЫЙ адаптер rentbot.adapters.playerok (тот же код, что и в проде), а не копию.

Переменные окружения:
  PLAYEROK_TOKEN         cookie `token` из DevTools (нужен для auth-режима)
  PLAYEROK_TEST_CHAT_ID  UUID чата, куда слать тестовое сообщение (для --send)
  PLAYEROK_PROXY         опционально, http://user:pass@host:port
  PLAYEROK_TEST_TEXT     опционально, текст сообщения

Режимы:
  python spike/playerok_check.py --selftest   # БЕЗ токена: проверка Cloudflare + публичный каталог + валидация схемы
  python spike/playerok_check.py              # чтение: viewer, чаты, сделки (печатает id чатов)
  python spike/playerok_check.py --send       # + отправка в PLAYEROK_TEST_CHAT_ID и проверка, что дошло

Критерий успеха основного спайка: тестовое сообщение дошло (видно при повторном чтении чата).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8")  # Windows-консоль по умолчанию cp1251
sys.stderr.reconfigure(encoding="utf-8")

from rentbot.adapters.playerok import PlayerokAdapter  # noqa: E402
from rentbot.vault import mask  # noqa: E402


def short(text: str | None, n: int = 60) -> str:
    t = (text or "").replace("\n", " ")
    return t if len(t) <= n else t[:n] + "…"


async def selftest() -> int:
    """Без токена: доказывает, что сетевой путь адаптера жив (Cloudflare + каталог + схема)."""
    ad = PlayerokAdapter(token="selftest-no-token", proxy=os.environ.get("PLAYEROK_PROXY"))
    from curl_cffi import requests as cffi
    ad._http = cffi.AsyncSession(impersonate="chrome", proxy=ad.proxy, timeout=30)
    try:
        await ad._warmup()
        print("[cf] Cloudflare пройден (GET / без челленджа)")

        cat = await ad.catalog(size=3)
        print(f"[catalog] POST /v1/catalog/items → {len(cat['items'])} лотов, "
              f"hasNextPage={cat['hasNextPage']}")
        if cat["items"]:
            it = cat["items"][0]
            print(f"   пример: «{short(it.get('name'), 40)}» {it.get('price')}₽ "
                  f"deals={it.get('dealsCounter')} seller={(it.get('seller') or {}).get('username')}")

        # Валидация текстов операций против живой схемы (выполнение падает на auth, но не на синтаксисе).
        ok = []
        for op in ("viewer", "deals", "chats", "chatMessages", "createChatMessage"):
            try:
                await ad.gql(op, _dummy_vars(op))
                ok.append(f"{op}:выполнилось")
            except Exception as e:
                msg = str(e)
                if "GRAPHQL_VALIDATION_FAILED" in msg or "Cannot query" in msg:
                    ok.append(f"{op}:СХЕМА-ОШИБКА")
                else:
                    ok.append(f"{op}:ok(схема валидна)")  # INTERNAL/forbidden = прошло валидацию
        print("[schema] " + ", ".join(ok))
        bad = [x for x in ok if "СХЕМА-ОШИБКА" in x]
        print("\nSELFTEST:", "FAIL" if bad else "OK — сетевой путь и все операции живы")
        return 1 if bad else 0
    finally:
        await ad.close()


def _dummy_vars(op: str) -> dict:
    z = "00000000-0000-0000-0000-000000000000"
    return {
        "viewer": {},
        "deals": {"pagination": {"first": 1}, "filter": {"userId": z}},
        "chats": {"pagination": {"first": 1}, "filter": {"userId": z}},
        "chatMessages": {"pagination": {"first": 1}, "filter": {"chatId": z}},
        "createChatMessage": {"input": {"chatId": z, "text": "selftest"}},
    }[op]


async def run_auth(send: bool) -> int:
    token = os.environ.get("PLAYEROK_TOKEN")
    if not token:
        print("Нет PLAYEROK_TOKEN в окружении (для self-test без токена: --selftest)")
        return 2
    chat_id = os.environ.get("PLAYEROK_TEST_CHAT_ID")
    print(f"[cfg] token={mask(token)} proxy={'да' if os.environ.get('PLAYEROK_PROXY') else 'нет'}")

    ad = PlayerokAdapter(token, proxy=os.environ.get("PLAYEROK_PROXY"))
    try:
        viewer = await ad.start()
        print(f"[auth] OK: {viewer['username']} (id {viewer['id']}), "
              f"непрочитанных чатов: {viewer['unreadChatsCounter']}")

        chats = (await ad.gql("chats", {"pagination": {"first": 10},
                                        "filter": {"userId": viewer['id']}}))["chats"]
        print(f"[chats] всего {chats['totalCount']}:")
        for e in chats["edges"]:
            c = e["node"]
            other = ", ".join(u["username"] for u in (c["participants"] or []) if u["id"] != viewer["id"])
            lm = c["lastMessage"] or {}
            print(f"   {c['id']}  {c['type']:<8} {c['status']:<10} unread={c['unreadMessagesCounter']}"
                  f"  с: {other or '-'}  «{short(lm.get('text'))}»")

        deals = (await ad.gql("deals", {"pagination": {"first": 5},
                                        "filter": {"userId": viewer['id']}}))["deals"]
        print(f"[deals] всего {deals['totalCount']}:")
        for e in deals["edges"]:
            d = e["node"]
            item = d["item"] or {}
            print(f"   {d['id']}  {d['status']:<10} {d['direction']:<4} {d['createdAt']}"
                  f"  «{short(item.get('name'), 35)}» {item.get('price')}₽"
                  f"  chat={(d['chat'] or {}).get('id')}")

        if not send:
            print("\nЧтение OK. Для отправки: PLAYEROK_TEST_CHAT_ID=<id из списка> ... --send")
            return 0
        if not chat_id:
            print("--send без PLAYEROK_TEST_CHAT_ID — наугад покупателям не пишем")
            return 2

        text = os.environ.get("PLAYEROK_TEST_TEXT") or \
            f"rentbot spike: тест {dt.datetime.now().isoformat(timespec='seconds')}"
        await ad.send_message(chat_id, text)
        print(f"[send] createChatMessage → отправлено в {chat_id}")
        await asyncio.sleep(3)

        msgs = (await ad.gql("chatMessages", {"pagination": {"first": 10},
                                              "filter": {"chatId": chat_id}}))["chatMessages"]
        found = any((e["node"].get("text") or "") == text for e in msgs["edges"])
        print(f"[verify] сообщение {'НАЙДЕНО' if found else 'НЕ найдено'} в чате")
        return 0 if found else 1
    finally:
        await ad.close()


def main() -> int:
    try:
        if "--selftest" in sys.argv:
            return asyncio.run(selftest())
        return asyncio.run(run_auth(send="--send" in sys.argv))
    except Exception as e:
        print(f"\n[FAIL] {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
