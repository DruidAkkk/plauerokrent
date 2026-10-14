"""Steam Guard: генерация 5-символьных кодов из shared_secret (как в мобильном приложении / SDA).

Алгоритм: TOTP (HMAC-SHA1, шаг 30 с) с особым алфавитом Steam.
shared_secret берётся из maFile (поле "shared_secret", base64).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

STEAM_ALPHABET = "23456789BCDFGHJKMNPQRTVWXY"
STEP_SECONDS = 30


def generate_code(shared_secret: str, timestamp: float | None = None) -> str:
    """Вернуть код Steam Guard для момента timestamp (по умолчанию — сейчас)."""
    if timestamp is None:
        timestamp = time.time()
    key = base64.b64decode(shared_secret)
    counter = int(timestamp) // STEP_SECONDS
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[19] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    chars = []
    for _ in range(5):
        value, idx = divmod(value, len(STEAM_ALPHABET))
        chars.append(STEAM_ALPHABET[idx])
    return "".join(chars)


def seconds_left(timestamp: float | None = None) -> int:
    """Сколько секунд текущий код ещё действителен."""
    if timestamp is None:
        timestamp = time.time()
    return STEP_SECONDS - int(timestamp) % STEP_SECONDS
