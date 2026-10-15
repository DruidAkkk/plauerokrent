"""Тесты предохранителя соответствия правилам площадки."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.compliance import ActionBudget, check


def test_policy():
    assert check("relist").ok and check("bump").ok and check("reprice").ok
    assert not check("create_item").ok and "запрещено" in check("create_item").reason
    assert not check("delete_item").ok
    assert not check("whatever").ok


def test_budget():
    t = [0.0]
    b = ActionBudget(2, 10, lambda: t[0])
    assert b.allow() and b.allow() and not b.allow()
    t[0] = 10.0
    assert b.allow()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in tests:
        f()
    print(f"{len(tests)}/{len(tests)} passed")
