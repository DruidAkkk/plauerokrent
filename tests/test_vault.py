"""Тесты env-ключа сейфа: детерминизм, round-trip, несовпадение секрета, работа Storage."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot.storage import Storage
from rentbot.vault import Vault, derive_env_key, env_configured


def test_derive_is_deterministic():
    k1 = derive_env_key("secret-A", "salt-X")
    k2 = derive_env_key("secret-A", "salt-X")
    k3 = derive_env_key("secret-B", "salt-X")
    assert k1 == k2, "один секрет+соль → один ключ"
    assert k1 != k3, "другой секрет → другой ключ"
    print("PASS test_derive_is_deterministic")


def test_env_roundtrip():
    v = Vault.from_env("my-secret", "my-salt")
    tok = v.encrypt("пароль123")
    assert v.decrypt(tok) == "пароль123"
    # тот же секрет/соль в другом экземпляре расшифровывает
    assert Vault.from_env("my-secret", "my-salt").decrypt(tok) == "пароль123"
    print("PASS test_env_roundtrip")


def test_wrong_secret_fails():
    tok = Vault.from_env("right", "salt").encrypt("x")
    try:
        Vault.from_env("wrong", "salt").decrypt(tok)
    except ValueError:
        print("PASS test_wrong_secret_fails")
        return
    raise AssertionError("расшифровка чужим секретом должна падать")


def test_env_configured_reads_environ():
    old = (os.environ.get("ENCRYPTION_SECRET"), os.environ.get("ENCRYPTION_SALT"))
    try:
        os.environ["ENCRYPTION_SECRET"] = "s"
        os.environ["ENCRYPTION_SALT"] = "t"
        assert env_configured() is True
        del os.environ["ENCRYPTION_SALT"]
        assert env_configured() is False
    finally:
        for k, v in zip(("ENCRYPTION_SECRET", "ENCRYPTION_SALT"), old):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("PASS test_env_configured_reads_environ")


def test_storage_with_env_vault():
    dbp = os.path.join(tempfile.mkdtemp(), "r.db")
    v = Vault.from_env("vps-secret", "vps-salt")
    s = Storage(dbp, v)
    s.set_setting("pl_token", "TOKEN-xyz", secret=True)
    s.db.close()
    # воркер на VPS откроет ту же БД тем же env-ключом
    s2 = Storage(dbp, Vault.from_env("vps-secret", "vps-salt"))
    assert s2.get_setting("pl_token") == "TOKEN-xyz"
    # и секрет лежит зашифрованным (в сырой строке токена нет)
    import sqlite3
    raw = sqlite3.connect(dbp).execute("SELECT v FROM settings WHERE k='pl_token'").fetchone()[0]
    assert "TOKEN-xyz" not in raw
    print("PASS test_storage_with_env_vault")


if __name__ == "__main__":
    import inspect
    tests = [fn for n, fn in sorted(globals().items())
             if n.startswith("test_") and inspect.isfunction(fn)]
    for t in tests:
        t()
    print(f"\n{len(tests)}/{len(tests)} passed")
