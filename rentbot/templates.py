"""Шаблоны ответов с плейсхолдерами и вариативностью (#11, #26).

Чистая логика без сети. Синтаксис:
  {name}            — подстановка поля (неизвестное поле остаётся как есть, без падения);
  [[вариант1|вариант2]] — случайный вариант фразы, чтобы ответы не выглядели одинаково.
Вариативность — только про естественность текста; ничего не скрывает от площадки.
"""
from __future__ import annotations

import random
import re

_VARIANTS = re.compile(r"\[\[([^\[\]]*)\]\]")
_FIELD = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def render(template: str, fields: dict | None = None, rng: random.Random | None = None) -> str:
    """Раскрывает варианты и подставляет поля. Значения полей не интерпретируются повторно."""
    rng = rng or random
    fields = fields or {}
    text = _VARIANTS.sub(lambda m: rng.choice(m.group(1).split("|")).strip(), template)
    return _FIELD.sub(lambda m: str(fields[m.group(1)]) if m.group(1) in fields else m.group(0), text)


def variants_count(template: str) -> int:
    """Сколько различных текстов даёт шаблон (для подсказки «добавьте вариантов»)."""
    n = 1
    for m in _VARIANTS.finditer(template):
        n *= len(m.group(1).split("|"))
    return n
