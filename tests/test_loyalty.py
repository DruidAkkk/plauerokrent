"""Тесты лесенки скидок постоянным арендаторам."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.loyalty import discount_percent, loyal_price


def test_tiers():
    assert [discount_percent(n) for n in (0, 2, 3, 5, 6, 9, 10, 99)] == [0, 0, 5, 5, 10, 10, 15, 15]


def test_price_rounding():
    assert loyal_price(100, 3) == 95.0
    assert loyal_price(99.99, 10) == 84.99


def test_floor():
    assert loyal_price(100, 10, floor=90) == 90


def test_custom_tiers_capped():
    assert discount_percent(5, tiers=((1, 80),)) == 30


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in tests:
        f()
    print(f"{len(tests)}/{len(tests)} passed")
