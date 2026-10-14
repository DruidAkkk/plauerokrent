"""Тесты телеграм-бота управления (без сети: транспорт — фейк).
Проверяем команды и что бот отвечает только авторизованному чату."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.storage import Storage
from rentbot.tg_control import TelegramControl
from rentbot.vault import Vault


def _fixture(with_kosell=True):
    import tempfile
    s = Storage(os.path.join(tempfile.mkdtemp(), "r.db"), Vault("pw", Vault.new_salt()))
    now = time.time()
    if with_kosell:
        s.set_setting("kosell_key", "kosell_dummy", secret=True)
        s.kosell_rental_add("UID1", "playerok", "D1", "C1", 77, "Raft", "LOG", 24,
                            "2026-10-13T00:00:00Z", "2026-10-14T00:00:00Z", "active", now)
    aid = s.mobile_add(game="pubg_mobile", login_type="facebook", login="acc@m.com",
                       password="p", email=None, email_password=None, imap_host=None,
                       proxy=None, note="", tags=[])
    s.mobile_rental_add("playerok", "D2", "C2", aid, "pubg_mobile", "acc@m.com",
                        now, now + 3600, "active")
    sent = []
    bot = TelegramControl(s, "token", "555", transport=lambda m, p: _fake(m, p, sent))
    return s, bot, sent


async def _fake(method, payload, sent):
    sent.append((method, payload))
    return {"ok": True, "result": []}


def run(c):
    return asyncio.run(c)


def test_help():
    _, bot, _ = _fixture()
    r = run(bot.handle("/help"))
    assert "/status" in r and "/balance" in r
    print("PASS test_help")


def test_status():
    _, bot, _ = _fixture()
    r = run(bot.handle("/status"))
    assert "Воркер работает" in r and "PUBG свободно" in r
    print("PASS test_status")


def test_stats():
    _, bot, _ = _fixture()
    r = run(bot.handle("/stats"))
    assert "Сегодня" in r and "KoSell" in r
    print("PASS test_stats")


def test_active_lists_rentals():
    _, bot, _ = _fixture()
    r = run(bot.handle("/active"))
    assert "Raft" in r and "acc@m.com" in r
    print("PASS test_active_lists_rentals")


def test_unknown_command_silent():
    _, bot, _ = _fixture()
    assert run(bot.handle("/foobar")) is None
    assert run(bot.handle("просто текст")) is None
    print("PASS test_unknown_command_silent")


def test_balance_without_key():
    _, bot, _ = _fixture(with_kosell=False)
    r = run(bot.handle("/balance"))
    assert "не подключён" in r
    print("PASS test_balance_without_key")


def test_only_authorized_chat():
    _, bot, sent = _fixture()
    # чужой чат — игнор
    run(bot._on_update({"message": {"chat": {"id": 999}, "text": "/status"}}))
    assert sent == [], "ответили чужому чату"
    # свой чат — ответ
    run(bot._on_update({"message": {"chat": {"id": 555}, "text": "/status"}}))
    assert len(sent) == 1 and sent[0][0] == "sendMessage"
    print("PASS test_only_authorized_chat")


if __name__ == "__main__":
    import inspect
    tests = [fn for n, fn in sorted(globals().items())
             if n.startswith("test_") and inspect.isfunction(fn)]
    for t in tests:
        t()
    print(f"\n{len(tests)}/{len(tests)} passed")
