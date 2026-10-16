"""Тесты оценки риска покупателя."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.buyer_risk import BuyerHistory as H, assess


def test_clean():
    assert assess(H(deals=5))[0] == "ok"
    assert assess(H())[0] == "ok"


def test_single_claim_ok():
    assert assess(H(deals=9, refunds=1))[0] == "ok"


def test_watch():
    assert assess(H(deals=8, disputes=2))[0] == "watch"
    assert assess(H(deals=1, refunds=1))[0] == "watch"


def test_risky():
    assert assess(H(deals=1, disputes=2, refunds=2))[0] == "risky"


def test_blacklist():
    lvl, why = assess(H(deals=50, blacklisted=True))
    assert lvl == "risky" and "чёрн" in why


if __name__ == "__main__":
    ts = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in ts:
        t()
    print(f"{len(ts)}/{len(ts)} passed")
