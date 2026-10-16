"""Тесты вежливой просьбы об отзыве."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rentbot.review_request as review_request
from rentbot.adapters.fakes import FakeMarket
from rentbot.review_request import ReviewAsker, TEMPLATE
from rentbot.storage import Storage
from rentbot.vault import Vault


def _storage():
    dbp = os.path.join(tempfile.mkdtemp(), "r.db")
    return Storage(dbp, Vault("pw", Vault.new_salt()))


def run(c):
    return asyncio.run(c)


def test_asks_once():
    a = ReviewAsker()
    assert a.message("o1", completed=True)
    assert a.message("o1", completed=True) is None
    assert a.message("o2", completed=True)


def test_not_asked_when_bad():
    a = ReviewAsker()
    assert a.message("o1", completed=False) is None
    assert a.message("o2", completed=True, disputed=True) is None
    assert a.message("o3", completed=True, refunded=True) is None
    assert a.message("o4", completed=True, already_reviewed=True) is None
    assert a.message("o1", completed=True)  # неудачные попытки не «сжигают» сделку


def test_no_incentive_wording():
    t = ReviewAsker().template.lower()
    assert "5" not in t and "скидк" not in t and "бонус" not in t


def test_disabled_by_default():
    s = _storage()
    assert review_request.should_ask(s) is False, "по умолчанию просьба выключена (opt-in)"
    mk = FakeMarket()
    assert run(review_request.ask_after_completion(s, mk, "C1", order_id="D1")) is False
    assert mk.sent == [], "выключено — ничего не шлём"


def test_enabled_sends_default_text():
    s = _storage()
    s.set_setting(review_request.ENABLED_KEY, "1")
    mk = FakeMarket()
    assert run(review_request.ask_after_completion(s, mk, "C1", order_id="D1")) is True
    assert mk.sent == [("C1", TEMPLATE)], "должен уйти текст по умолчанию"


def test_enabled_uses_custom_text():
    s = _storage()
    s.set_setting(review_request.ENABLED_KEY, "1")
    s.set_setting(review_request.TEXT_KEY, "Оцените, пожалуйста, сделку 🙏")
    mk = FakeMarket()
    run(review_request.ask_after_completion(s, mk, "C1", order_id="D1"))
    assert mk.sent == [("C1", "Оцените, пожалуйста, сделку 🙏")]


def test_no_chat_no_send():
    s = _storage()
    s.set_setting(review_request.ENABLED_KEY, "1")
    mk = FakeMarket()
    assert run(review_request.ask_after_completion(s, mk, None, order_id="D1")) is False
    assert mk.sent == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in tests:
        f()
    print(f"{len(tests)}/{len(tests)} passed")
