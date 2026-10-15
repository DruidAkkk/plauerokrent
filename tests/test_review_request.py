"""Тесты вежливой просьбы об отзыве."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.review_request import ReviewAsker


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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in tests:
        f()
    print(f"{len(tests)}/{len(tests)} passed")
