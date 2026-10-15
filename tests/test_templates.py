"""Тесты шаблонов ответов."""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.templates import render, variants_count


def test_fields():
    assert render("Привет, {buyer}! Заказ {order}", {"buyer": "Ян", "order": 5}) == "Привет, Ян! Заказ 5"


def test_unknown_field_kept():
    assert render("Код {code}", {}) == "Код {code}"


def test_variants_and_count():
    t = "[[Здравствуйте|Привет]], [[спасибо|благодарю]]!"
    assert variants_count(t) == 4
    seen = {render(t, rng=random.Random(i)) for i in range(50)}
    assert seen <= {"Здравствуйте, спасибо!", "Здравствуйте, благодарю!", "Привет, спасибо!", "Привет, благодарю!"}
    assert len(seen) > 1


def test_field_value_not_reinterpreted():
    assert render("{a}", {"a": "[[x|y]]{b}"}) == "[[x|y]]{b}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in tests:
        f()
    print(f"{len(tests)}/{len(tests)} passed")
