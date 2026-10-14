"""Шифрование секретов (пароли, maFile, токены) на машине/VPS продавца.

Два способа получить ключ шифрования — оба НИКОГДА не покидают сервер продавца:
  • Мастер-пароль (scrypt) — интерактивный вход в десктоп-панели.
  • env-ключ (PBKDF2-HMAC-SHA256, 600k итераций → Fernet) — для headless-воркера на VPS без
    ввода пароля. Секрет и соль берутся из переменных окружения ENCRYPTION_SECRET / ENCRYPTION_SALT,
    которые задаёт САМ клиент на своей VPS. Разработчик их не знает и расшифровать данные не может.

Секреты никогда не печатаются в логи (см. mask).
"""
from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

ENV_SECRET = "ENCRYPTION_SECRET"
ENV_SALT = "ENCRYPTION_SALT"
PBKDF2_ITERATIONS = 600_000


class Vault:
    def __init__(self, master_password: str, salt: bytes):
        kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
        key = base64.urlsafe_b64encode(kdf.derive(master_password.encode()))
        self._f = Fernet(key)

    @classmethod
    def from_key(cls, key: bytes) -> "Vault":
        """Сконструировать сейф из готового Fernet-ключа (минуя KDF мастер-пароля)."""
        self = cls.__new__(cls)
        self._f = Fernet(key)
        return self

    @classmethod
    def from_env(cls, secret: str | None = None, salt: str | None = None) -> "Vault":
        """Сейф с ключом из env (PBKDF2). Используется headless-воркером на VPS."""
        return cls.from_key(derive_env_key(secret, salt))

    @staticmethod
    def new_salt() -> bytes:
        return os.urandom(16)

    def encrypt(self, value: str | None) -> str | None:
        if value is None:
            return None
        return self._f.encrypt(value.encode()).decode()

    def decrypt(self, token: str | None) -> str | None:
        if token is None:
            return None
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise ValueError("Неверный ключ (мастер-пароль/env-секрет) или повреждённые данные") from e


def env_configured() -> bool:
    """Заданы ли обе переменные окружения для env-ключа."""
    return bool(os.getenv(ENV_SECRET) and os.getenv(ENV_SALT))


def derive_env_key(secret: str | None = None, salt: str | None = None) -> bytes:
    """Вывести Fernet-ключ из секрета+соли через PBKDF2-HMAC-SHA256 (600k итераций).

    secret/salt берутся из аргументов или из env (ENCRYPTION_SECRET / ENCRYPTION_SALT).
    """
    secret = secret if secret is not None else os.getenv(ENV_SECRET)
    salt = salt if salt is not None else os.getenv(ENV_SALT)
    if not secret or not salt:
        raise RuntimeError(f"Нужны переменные окружения {ENV_SECRET} и {ENV_SALT}")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=salt.encode(), iterations=PBKDF2_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(secret.encode()))


def mask(secret: str | None) -> str:
    """Для логов: никогда не печатаем секреты целиком."""
    if not secret:
        return "∅"
    return secret[:2] + "…" + secret[-1:] if len(secret) > 4 else "***"
