"""Хранилище на SQLite (stdlib). Один файл БД у продавца, WAL-режим.

Секретные поля шифруются Vault'ом прозрачно для остального кода.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .domain import (Account, AccountState, LotBinding, Rental, RentalState,
                     RotationMode)
from .vault import Vault

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL, login TEXT NOT NULL UNIQUE,
    password_enc TEXT NOT NULL, shared_secret_enc TEXT, identity_secret_enc TEXT,
    mafile_enc TEXT, proxy TEXT,
    state TEXT NOT NULL DEFAULT 'free', rest_until REAL NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS lots (
    marketplace TEXT NOT NULL, lot_id TEXT NOT NULL, game TEXT NOT NULL,
    hours REAL NOT NULL, rotation TEXT NOT NULL DEFAULT 'auto', title TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (marketplace, lot_id)
);
CREATE TABLE IF NOT EXISTS rentals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL, deal_id TEXT NOT NULL, lot_id TEXT NOT NULL,
    buyer_id TEXT NOT NULL, buyer_name TEXT NOT NULL, chat_id TEXT NOT NULL,
    account_id INTEGER, state TEXT NOT NULL,
    started_at REAL NOT NULL, ends_at REAL NOT NULL, price REAL NOT NULL DEFAULT 0,
    warned INTEGER NOT NULL DEFAULT 0, codes_issued INTEGER NOT NULL DEFAULT 0,
    UNIQUE (marketplace, deal_id)
);
CREATE INDEX IF NOT EXISTS rentals_state ON rentals(state, ends_at);
CREATE TABLE IF NOT EXISTS blacklist (
    scope TEXT NOT NULL DEFAULT 'steam',     -- steam | kosell | mobile: у каждой аренды свой ЧС
    marketplace TEXT NOT NULL, buyer_id TEXT NOT NULL, reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (scope, marketplace, buyer_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    kind TEXT NOT NULL, rental_id INTEGER, account_id INTEGER, data TEXT
);

-- ===== базовые функции площадки (бесплатный слой: не про аренду) =====
-- Настройки приложения (Telegram-токен и пр.). Секреты — с префиксом 'enc:' (Fernet).
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);

-- Автовыдача: что отдавать покупателю по лоту после оплаты.
CREATE TABLE IF NOT EXISTS delivery (
    marketplace TEXT NOT NULL, lot_id TEXT NOT NULL,
    mode TEXT NOT NULL,            -- text | keys | file
    template TEXT NOT NULL DEFAULT '',  -- текст-шаблон (text) или путь к файлу (file)
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (marketplace, lot_id)
);
-- Склад одноразовых выдач для mode=keys (ключи/логин-пароль), по одному на заказ. Шифруется.
CREATE TABLE IF NOT EXISTS stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL, lot_id TEXT NOT NULL,
    payload_enc TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
    used_at REAL NOT NULL DEFAULT 0, deal_id TEXT
);
-- Защита от повторной выдачи: одна выдача на сделку (идемпотентность автовыдачи).
CREATE TABLE IF NOT EXISTS delivered (
    marketplace TEXT NOT NULL, deal_id TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (marketplace, deal_id)
);
-- Автоответчик: приветствие и правила по ключевым словам.
CREATE TABLE IF NOT EXISTS autoresponses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- greeting | keyword
    pattern TEXT NOT NULL DEFAULT '',   -- для keyword: подстрока/слово (lower)
    reply TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
-- Кому уже отправляли приветствие (чтобы не спамить).
CREATE TABLE IF NOT EXISTS greeted (
    marketplace TEXT NOT NULL, chat_id TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (marketplace, chat_id)
);
-- Автоподнятие/перевыставление лотов (по умолчанию выключено — антибан).
CREATE TABLE IF NOT EXISTS bump (
    marketplace TEXT NOT NULL, lot_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    interval_sec REAL NOT NULL DEFAULT 14400,  -- 4 ч по умолчанию
    last_bump REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (marketplace, lot_id)
);
-- Плагин KoSell: соответствие «лот Playerok → игра KoSell» и журнал проведённых аренд.
CREATE TABLE IF NOT EXISTS kosell_map (
    marketplace TEXT NOT NULL,
    lot_id TEXT NOT NULL,                       -- id лота на Playerok
    product_id INTEGER NOT NULL,                -- id игры в каталоге KoSell
    product_name TEXT NOT NULL DEFAULT '',
    hours INTEGER NOT NULL DEFAULT 24,          -- сколько часов выдаём за один заказ
    markup_percent REAL NOT NULL DEFAULT 100,   -- наценка к цене KoSell (для репрайсера)
    currency TEXT NOT NULL DEFAULT 'RUB',
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (marketplace, lot_id)
);
CREATE TABLE IF NOT EXISTS kosell_rentals (
    rental_uid TEXT PRIMARY KEY,                -- id аренды на стороне KoSell
    marketplace TEXT NOT NULL DEFAULT 'playerok',
    deal_id TEXT,                               -- заказ Playerok (идемпотентность)
    chat_id TEXT,                               -- чат с покупателем (для !код/!время)
    product_id INTEGER,
    product_name TEXT DEFAULT '',
    steam_login TEXT DEFAULT '',
    hours INTEGER DEFAULT 0,
    started_at TEXT DEFAULT '',
    expires_at TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_kosell_rentals_deal ON kosell_rentals (marketplace, deal_id);
CREATE INDEX IF NOT EXISTS ix_kosell_rentals_chat ON kosell_rentals (marketplace, chat_id);
-- Плагин «Аренда PUBG Mobile»: пул мобильных аккаунтов (соц-логин + подконтрольная почта).
CREATE TABLE IF NOT EXISTS mobile_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL DEFAULT 'pubg_mobile',
    login_type TEXT NOT NULL DEFAULT 'facebook',  -- facebook|google|twitter|vk|apple|email
    login TEXT NOT NULL,                           -- идентификатор, который видит покупатель
    password_enc TEXT,                             -- пароль соц-логина (шифр.)
    email TEXT,                                    -- подконтрольная почта (для кодов/возврата)
    email_password_enc TEXT,                       -- пароль почты для чтения кодов (шифр.)
    imap_host TEXT,                                -- IMAP-хост (иначе выводим из домена)
    proxy TEXT,
    state TEXT NOT NULL DEFAULT 'free',            -- free|rented|paused
    rest_until REAL NOT NULL DEFAULT 0,
    note TEXT DEFAULT '',
    tags TEXT DEFAULT '[]'
);
-- Лоты PUBG Mobile: привязка лота Playerok к пулу мобильных аккаунтов (по метке game).
CREATE TABLE IF NOT EXISTS mobile_lots (
    marketplace TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    game TEXT NOT NULL DEFAULT 'pubg_mobile',   -- какой пул обслуживает лот
    hours INTEGER NOT NULL DEFAULT 24,          -- срок аренды за один заказ
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (marketplace, lot_id)
);
-- Аренды PUBG Mobile: что кому выдано и до когда.
CREATE TABLE IF NOT EXISTS mobile_rentals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL DEFAULT 'playerok',
    deal_id TEXT,
    chat_id TEXT,
    account_id INTEGER NOT NULL,
    game TEXT DEFAULT 'pubg_mobile',
    login TEXT DEFAULT '',
    started REAL NOT NULL DEFAULT 0,
    expires REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'        -- active|expired|terminated
);
CREATE INDEX IF NOT EXISTS ix_mobile_rentals_deal ON mobile_rentals (marketplace, deal_id);
CREATE INDEX IF NOT EXISTS ix_mobile_rentals_chat ON mobile_rentals (marketplace, chat_id);
-- Плагин «Гарантия/Автозамена»: журнал гарантийных случаев (нет стока, замена, жалоба).
CREATE TABLE IF NOT EXISTS warranty (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    marketplace TEXT NOT NULL DEFAULT 'playerok',
    deal_id TEXT,
    lot_id TEXT,
    product TEXT DEFAULT '',
    reason TEXT NOT NULL,       -- no_stock | buyer_replace | rent_error
    action TEXT NOT NULL,       -- retried | resent_creds | notified | ...
    status TEXT NOT NULL DEFAULT 'open'  -- open | resolved
);
"""


class Storage:
    def __init__(self, path: str | Path, vault: Vault):
        # check_same_thread=False: pywebview зовёт методы из разных потоков WebView2.
        # Безопасность обеспечивает сериализация доступа (Backend._lock); воркер держит
        # отдельное подключение в своём потоке.
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")  # панель и воркер пишут в один файл (WAL)
        self.db.executescript(SCHEMA)
        self._migrate()
        self.vault = vault

    def _migrate(self) -> None:
        """Лёгкие миграции для существующих БД (CREATE TABLE IF NOT EXISTS новые колонки не добавляет)."""
        bl_cols = {r[1] for r in self.db.execute("PRAGMA table_info(blacklist)")}
        if "scope" not in bl_cols:
            # переход на ЧС-по-аренде: пересобираем таблицу со scope в ключе. Старые СВОИ активные
            # записи переносим в ЧС Steam-аренды; записи общего ЧС (source=global) отбрасываем.
            conds = []
            if "enabled" in bl_cols:
                conds.append("enabled=1")
            if "source" in bl_cols:
                conds.append("source!='global'")
            where = (" WHERE " + " AND ".join(conds)) if conds else ""
            self.db.execute(
                "CREATE TABLE blacklist_new ("
                " scope TEXT NOT NULL DEFAULT 'steam', marketplace TEXT NOT NULL,"
                " buyer_id TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL,"
                " PRIMARY KEY (scope, marketplace, buyer_id))")
            self.db.execute(
                "INSERT OR IGNORE INTO blacklist_new (scope, marketplace, buyer_id, reason, created_at) "
                "SELECT 'steam', marketplace, buyer_id, reason, created_at FROM blacklist" + where)
            self.db.execute("DROP TABLE blacklist")
            self.db.execute("ALTER TABLE blacklist_new RENAME TO blacklist")

    # ---------- accounts ----------
    def add_account(self, acc: Account) -> int:
        v = self.vault
        cur = self.db.execute(
            "INSERT INTO accounts (game, login, password_enc, shared_secret_enc, identity_secret_enc,"
            " mafile_enc, proxy, state, rest_until, note, tags) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (acc.game, acc.login, v.encrypt(acc.password), v.encrypt(acc.shared_secret),
             v.encrypt(acc.identity_secret), v.encrypt(acc.mafile_json), acc.proxy,
             acc.state.value, acc.rest_until, acc.note, json.dumps(acc.tags)))
        acc.id = cur.lastrowid
        return acc.id

    def _row_to_account(self, r: sqlite3.Row) -> Account:
        v = self.vault
        return Account(
            id=r["id"], game=r["game"], login=r["login"], password=v.decrypt(r["password_enc"]),
            shared_secret=v.decrypt(r["shared_secret_enc"]),
            identity_secret=v.decrypt(r["identity_secret_enc"]),
            mafile_json=v.decrypt(r["mafile_enc"]), proxy=r["proxy"],
            state=AccountState(r["state"]), rest_until=r["rest_until"], note=r["note"],
            tags=json.loads(r["tags"]))

    def get_account(self, account_id: int) -> Account:
        r = self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if r is None:
            raise KeyError(account_id)
        return self._row_to_account(r)

    def list_accounts(self, game: str | None = None) -> list[Account]:
        if game:
            rows = self.db.execute("SELECT * FROM accounts WHERE game=? ORDER BY id", (game,))
        else:
            rows = self.db.execute("SELECT * FROM accounts ORDER BY id")
        return [self._row_to_account(r) for r in rows]

    def set_account_state(self, account_id: int, state: AccountState, rest_until: float = 0.0,
                          note: str | None = None) -> None:
        if note is None:
            self.db.execute("UPDATE accounts SET state=?, rest_until=? WHERE id=?",
                            (state.value, rest_until, account_id))
        else:
            self.db.execute("UPDATE accounts SET state=?, rest_until=?, note=? WHERE id=?",
                            (state.value, rest_until, note, account_id))

    def set_password(self, account_id: int, password: str) -> None:
        self.db.execute("UPDATE accounts SET password_enc=? WHERE id=?",
                        (self.vault.encrypt(password), account_id))

    def update_account_fields(self, account_id: int, game: str, proxy: str | None,
                              note: str, tags: list[str]) -> None:
        self.db.execute("UPDATE accounts SET game=?, proxy=?, note=?, tags=? WHERE id=?",
                        (game, proxy, note, json.dumps(tags), account_id))

    def update_account_secrets(self, account_id: int, shared_secret: str | None,
                               identity_secret: str | None, mafile_json: str | None) -> None:
        v = self.vault
        self.db.execute("UPDATE accounts SET shared_secret_enc=?, identity_secret_enc=?, mafile_enc=? WHERE id=?",
                        (v.encrypt(shared_secret), v.encrypt(identity_secret), v.encrypt(mafile_json), account_id))

    def delete_account(self, account_id: int) -> None:
        self.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def claim_free_account(self, game: str, now: float) -> Account | None:
        """Атомарно взять свободный аккаунт пула (с учётом «отдыха»)."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # отдохнувшие возвращаем в оборот
            self.db.execute("UPDATE accounts SET state='free', rest_until=0 "
                            "WHERE state='resting' AND rest_until<=?", (now,))
            r = self.db.execute("SELECT * FROM accounts WHERE game=? AND state='free' "
                                "ORDER BY id LIMIT 1", (game,)).fetchone()
            if r is None:
                self.db.execute("COMMIT")
                return None
            self.db.execute("UPDATE accounts SET state='rented' WHERE id=?", (r["id"],))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        acc = self._row_to_account(r)
        acc.state = AccountState.RENTED
        return acc

    def free_count(self, game: str, now: float) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM accounts WHERE game=? AND (state='free' OR "
            "(state='resting' AND rest_until<=?))", (game, now)).fetchone()[0]

    def resting_due(self, now: float) -> list[Account]:
        rows = self.db.execute("SELECT * FROM accounts WHERE state='resting' AND rest_until<=?", (now,))
        return [self._row_to_account(r) for r in rows]

    # ---------- lots ----------
    def bind_lot(self, lot: LotBinding) -> None:
        self.db.execute("INSERT OR REPLACE INTO lots VALUES (?,?,?,?,?,?)",
                        (lot.marketplace, lot.lot_id, lot.game, lot.hours, lot.rotation.value, lot.title))

    def get_lot(self, marketplace: str, lot_id: str) -> LotBinding | None:
        r = self.db.execute("SELECT * FROM lots WHERE marketplace=? AND lot_id=?",
                            (marketplace, lot_id)).fetchone()
        if r is None:
            return None
        return LotBinding(r["marketplace"], r["lot_id"], r["game"], r["hours"],
                          RotationMode(r["rotation"]), r["title"])

    def lots_for_game(self, game: str) -> list[LotBinding]:
        rows = self.db.execute("SELECT * FROM lots WHERE game=?", (game,))
        return [LotBinding(r["marketplace"], r["lot_id"], r["game"], r["hours"],
                           RotationMode(r["rotation"]), r["title"]) for r in rows]

    # ---------- rentals ----------
    def _row_to_rental(self, r: sqlite3.Row) -> Rental:
        return Rental(id=r["id"], marketplace=r["marketplace"], deal_id=r["deal_id"], lot_id=r["lot_id"],
                      buyer_id=r["buyer_id"], buyer_name=r["buyer_name"], chat_id=r["chat_id"],
                      account_id=r["account_id"], state=RentalState(r["state"]),
                      started_at=r["started_at"], ends_at=r["ends_at"], price=r["price"],
                      warned=bool(r["warned"]), codes_issued=r["codes_issued"])

    def insert_rental(self, rent: Rental) -> Rental | None:
        """Вставить аренду. None — если такая сделка уже обработана (идемпотентность)."""
        try:
            cur = self.db.execute(
                "INSERT INTO rentals (marketplace, deal_id, lot_id, buyer_id, buyer_name, chat_id, account_id,"
                " state, started_at, ends_at, price, warned, codes_issued) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rent.marketplace, rent.deal_id, rent.lot_id, rent.buyer_id, rent.buyer_name, rent.chat_id,
                 rent.account_id, rent.state.value, rent.started_at, rent.ends_at, rent.price,
                 int(rent.warned), rent.codes_issued))
        except sqlite3.IntegrityError:
            return None
        rent.id = cur.lastrowid
        return rent

    def update_rental(self, rent: Rental) -> None:
        self.db.execute(
            "UPDATE rentals SET account_id=?, state=?, started_at=?, ends_at=?, warned=?, codes_issued=? WHERE id=?",
            (rent.account_id, rent.state.value, rent.started_at, rent.ends_at, int(rent.warned),
             rent.codes_issued, rent.id))

    def active_rental_for_buyer(self, marketplace: str, buyer_id: str, game: str | None = None) -> Rental | None:
        q = ("SELECT r.* FROM rentals r JOIN lots l ON l.marketplace=r.marketplace AND l.lot_id=r.lot_id "
             "WHERE r.marketplace=? AND r.buyer_id=? AND r.state='active'")
        args: list = [marketplace, buyer_id]
        if game:
            q += " AND l.game=?"
            args.append(game)
        r = self.db.execute(q + " ORDER BY r.ends_at DESC LIMIT 1", args).fetchone()
        return self._row_to_rental(r) if r else None

    def active_rental_for_chat(self, marketplace: str, chat_id: str) -> Rental | None:
        r = self.db.execute("SELECT * FROM rentals WHERE marketplace=? AND chat_id=? AND state='active' "
                            "ORDER BY ends_at DESC LIMIT 1", (marketplace, chat_id)).fetchone()
        return self._row_to_rental(r) if r else None

    def rentals_by_state(self, state: RentalState) -> list[Rental]:
        rows = self.db.execute("SELECT * FROM rentals WHERE state=? ORDER BY started_at", (state.value,))
        return [self._row_to_rental(r) for r in rows]

    def queued_for_game(self, game: str) -> list[Rental]:
        rows = self.db.execute(
            "SELECT r.* FROM rentals r JOIN lots l ON l.marketplace=r.marketplace AND l.lot_id=r.lot_id "
            "WHERE r.state='queued' AND l.game=? ORDER BY r.id", (game,))
        return [self._row_to_rental(r) for r in rows]

    # ---------- blacklist (у каждой аренды свой ЧС: scope = steam | kosell | mobile) ----------
    BL_SCOPES = ("steam", "kosell", "mobile")

    def is_blacklisted(self, scope: str, marketplace: str, buyer_id: str) -> bool:
        """Заблокирован ли покупатель в ЧС конкретной аренды. У каждой аренды свой независимый
        список — блок в одной (напр. steam) не влияет на другие (kosell/mobile)."""
        return self.db.execute(
            "SELECT 1 FROM blacklist WHERE scope=? AND marketplace=? AND buyer_id=?",
            (scope, marketplace, buyer_id)).fetchone() is not None

    def blacklist_add(self, scope: str, marketplace: str, buyer_id: str, reason: str) -> None:
        """Добавить покупателя в ЧС конкретной аренды (идемпотентно, обновляет причину)."""
        self.db.execute(
            "INSERT INTO blacklist (scope, marketplace, buyer_id, reason, created_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(scope, marketplace, buyer_id) DO UPDATE SET "
            "reason=excluded.reason",
            (scope, marketplace, buyer_id, reason, time.time()))

    def blacklist_remove(self, scope: str, marketplace: str, buyer_id: str) -> None:
        self.db.execute("DELETE FROM blacklist WHERE scope=? AND marketplace=? AND buyer_id=?",
                        (scope, marketplace, buyer_id))

    def blacklist_list(self, scope: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM blacklist WHERE scope=? ORDER BY created_at DESC", (scope,))
        return [{"marketplace": r["marketplace"], "buyer_id": r["buyer_id"],
                 "reason": r["reason"], "created_at": r["created_at"]} for r in rows]

    def log(self, kind: str, rental_id: int | None = None, account_id: int | None = None, **data) -> None:
        self.db.execute("INSERT INTO events (ts, kind, rental_id, account_id, data) VALUES (?,?,?,?,?)",
                        (time.time(), kind, rental_id, account_id, json.dumps(data, ensure_ascii=False)))

    # ---------- settings (k/v, секреты шифруются) ----------
    def set_setting(self, key: str, value: str | None, secret: bool = False) -> None:
        if value is None:
            self.db.execute("DELETE FROM settings WHERE k=?", (key,))
            return
        stored = ("enc:" + self.vault.encrypt(value)) if secret else value
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, stored))

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        r = self.db.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
        if r is None:
            return default
        v = r["v"]
        return self.vault.decrypt(v[4:]) if v.startswith("enc:") else v

    # ---------- автовыдача ----------
    def set_delivery(self, marketplace: str, lot_id: str, mode: str,
                     template: str = "", enabled: bool = True) -> None:
        self.db.execute("INSERT OR REPLACE INTO delivery VALUES (?,?,?,?,?)",
                        (marketplace, lot_id, mode, template, int(enabled)))

    def get_delivery(self, marketplace: str, lot_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM delivery WHERE marketplace=? AND lot_id=?",
                            (marketplace, lot_id)).fetchone()
        if r is None:
            return None
        return {"marketplace": r["marketplace"], "lot_id": r["lot_id"], "mode": r["mode"],
                "template": r["template"], "enabled": bool(r["enabled"])}

    def list_delivery(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM delivery ORDER BY lot_id")
        return [{"marketplace": r["marketplace"], "lot_id": r["lot_id"], "mode": r["mode"],
                 "template": r["template"], "enabled": bool(r["enabled"])} for r in rows]

    def add_stock(self, marketplace: str, lot_id: str, payloads: list[str]) -> int:
        n = 0
        for p in payloads:
            if not p.strip():
                continue
            self.db.execute("INSERT INTO stock (marketplace, lot_id, payload_enc) VALUES (?,?,?)",
                            (marketplace, lot_id, self.vault.encrypt(p)))
            n += 1
        return n

    def stock_count(self, marketplace: str, lot_id: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM stock WHERE marketplace=? AND lot_id=? AND used=0",
                               (marketplace, lot_id)).fetchone()[0]

    def claim_stock(self, marketplace: str, lot_id: str, deal_id: str, now: float) -> str | None:
        """Атомарно взять один неиспользованный ключ и пометить использованным."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            r = self.db.execute("SELECT id, payload_enc FROM stock WHERE marketplace=? AND lot_id=? "
                                "AND used=0 ORDER BY id LIMIT 1", (marketplace, lot_id)).fetchone()
            if r is None:
                self.db.execute("COMMIT")
                return None
            self.db.execute("UPDATE stock SET used=1, used_at=?, deal_id=? WHERE id=?",
                            (now, deal_id, r["id"]))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.vault.decrypt(r["payload_enc"])

    def mark_delivered(self, marketplace: str, deal_id: str, now: float) -> bool:
        """True — выдали впервые; False — уже выдавали (идемпотентность)."""
        try:
            self.db.execute("INSERT INTO delivered VALUES (?,?,?)", (marketplace, deal_id, now))
            return True
        except sqlite3.IntegrityError:
            return False

    # ---------- автоответчик ----------
    def add_autoresponse(self, kind: str, reply: str, pattern: str = "", enabled: bool = True) -> int:
        cur = self.db.execute("INSERT INTO autoresponses (kind, pattern, reply, enabled) VALUES (?,?,?,?)",
                              (kind, pattern, reply, int(enabled)))
        return cur.lastrowid

    def list_autoresponses(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM autoresponses ORDER BY id")
        return [{"id": r["id"], "kind": r["kind"], "pattern": r["pattern"],
                 "reply": r["reply"], "enabled": bool(r["enabled"])} for r in rows]

    def delete_autoresponse(self, rid: int) -> None:
        self.db.execute("DELETE FROM autoresponses WHERE id=?", (rid,))

    def mark_greeted(self, marketplace: str, chat_id: str, now: float) -> bool:
        """True — приветствуем впервые; False — уже приветствовали."""
        try:
            self.db.execute("INSERT INTO greeted VALUES (?,?,?)", (marketplace, chat_id, now))
            return True
        except sqlite3.IntegrityError:
            return False

    # ---------- автоподнятие ----------
    def set_bump(self, marketplace: str, lot_id: str, enabled: bool, interval_sec: float) -> None:
        self.db.execute(
            "INSERT INTO bump (marketplace, lot_id, enabled, interval_sec) VALUES (?,?,?,?) "
            "ON CONFLICT(marketplace, lot_id) DO UPDATE SET enabled=excluded.enabled, "
            "interval_sec=excluded.interval_sec",
            (marketplace, lot_id, int(enabled), interval_sec))

    def list_bump(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM bump ORDER BY lot_id")
        return [{"marketplace": r["marketplace"], "lot_id": r["lot_id"], "enabled": bool(r["enabled"]),
                 "interval_sec": r["interval_sec"], "last_bump": r["last_bump"]} for r in rows]

    def bump_due(self, now: float) -> list[dict]:
        rows = self.db.execute("SELECT * FROM bump WHERE enabled=1 AND last_bump + interval_sec <= ?", (now,))
        return [{"marketplace": r["marketplace"], "lot_id": r["lot_id"],
                 "interval_sec": r["interval_sec"], "last_bump": r["last_bump"]} for r in rows]

    def mark_bumped(self, marketplace: str, lot_id: str, now: float) -> None:
        self.db.execute("UPDATE bump SET last_bump=? WHERE marketplace=? AND lot_id=?",
                        (now, marketplace, lot_id))

    # ---------- KoSell: соответствие лот↔игра ----------
    def kosell_map_set(self, marketplace: str, lot_id: str, product_id: int, product_name: str,
                       hours: int, markup_percent: float, currency: str, enabled: bool) -> None:
        self.db.execute(
            "INSERT INTO kosell_map (marketplace, lot_id, product_id, product_name, hours, "
            "markup_percent, currency, enabled) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(marketplace, lot_id) DO UPDATE SET product_id=excluded.product_id, "
            "product_name=excluded.product_name, hours=excluded.hours, "
            "markup_percent=excluded.markup_percent, currency=excluded.currency, enabled=excluded.enabled",
            (marketplace, lot_id, int(product_id), product_name, int(hours),
             float(markup_percent), currency, int(enabled)))

    def kosell_map_list(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM kosell_map ORDER BY product_name")
        return [dict(r) | {"enabled": bool(r["enabled"])} for r in rows]

    def kosell_map_get(self, marketplace: str, lot_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM kosell_map WHERE marketplace=? AND lot_id=?",
                            (marketplace, lot_id)).fetchone()
        return (dict(r) | {"enabled": bool(r["enabled"])}) if r else None

    def kosell_map_delete(self, marketplace: str, lot_id: str) -> None:
        self.db.execute("DELETE FROM kosell_map WHERE marketplace=? AND lot_id=?", (marketplace, lot_id))

    # ---------- KoSell: журнал проведённых аренд ----------
    def kosell_rental_add(self, rental_uid: str, marketplace: str, deal_id: str | None,
                          chat_id: str | None, product_id: int, product_name: str,
                          steam_login: str, hours: int, started_at: str, expires_at: str,
                          status: str, now: float) -> None:
        self.db.execute(
            "INSERT INTO kosell_rentals (rental_uid, marketplace, deal_id, chat_id, product_id, "
            "product_name, steam_login, hours, started_at, expires_at, status, created) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rental_uid) DO UPDATE SET "
            "status=excluded.status, expires_at=excluded.expires_at",
            (rental_uid, marketplace, deal_id, chat_id, int(product_id), product_name,
             steam_login, int(hours), started_at, expires_at, status, now))

    def kosell_rental_by_deal(self, marketplace: str, deal_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM kosell_rentals WHERE marketplace=? AND deal_id=?",
                            (marketplace, deal_id)).fetchone()
        return dict(r) if r else None

    def kosell_rental_by_chat(self, marketplace: str, chat_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM kosell_rentals WHERE marketplace=? AND chat_id=? "
                            "ORDER BY created DESC LIMIT 1", (marketplace, chat_id)).fetchone()
        return dict(r) if r else None

    def kosell_rental_list(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT * FROM kosell_rentals ORDER BY created DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def kosell_rental_update_status(self, rental_uid: str, status: str, expires_at: str | None = None) -> None:
        if expires_at is not None:
            self.db.execute("UPDATE kosell_rentals SET status=?, expires_at=? WHERE rental_uid=?",
                            (status, expires_at, rental_uid))
        else:
            self.db.execute("UPDATE kosell_rentals SET status=? WHERE rental_uid=?", (status, rental_uid))

    # ---------- гарантия/автозамена ----------
    def warranty_add(self, marketplace: str, deal_id: str | None, lot_id: str | None,
                     product: str, reason: str, action: str, now: float, status: str = "open") -> int:
        cur = self.db.execute(
            "INSERT INTO warranty (ts, marketplace, deal_id, lot_id, product, reason, action, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now, marketplace, deal_id, lot_id, product, reason, action, status))
        return cur.lastrowid

    def warranty_list(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT * FROM warranty ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def warranty_resolve(self, case_id: int) -> None:
        self.db.execute("UPDATE warranty SET status='resolved' WHERE id=?", (int(case_id),))

    # ---------- мобильные аккаунты (PUBG Mobile) ----------
    def mobile_add(self, *, game: str, login_type: str, login: str, password: str | None,
                   email: str | None, email_password: str | None, imap_host: str | None,
                   proxy: str | None, note: str, tags: list[str]) -> int:
        v = self.vault
        cur = self.db.execute(
            "INSERT INTO mobile_accounts (game, login_type, login, password_enc, email, "
            "email_password_enc, imap_host, proxy, note, tags) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (game, login_type, login, v.encrypt(password), email, v.encrypt(email_password),
             imap_host, proxy, note, json.dumps(tags)))
        return cur.lastrowid

    @staticmethod
    def _mask_email(e: str | None) -> str:
        if not e or "@" not in e:
            return e or ""
        name, dom = e.split("@", 1)
        head = name[:2] if len(name) > 2 else name[:1]
        return f"{head}***@{dom}"

    def mobile_list(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM mobile_accounts ORDER BY id DESC")
        out = []
        for r in rows:
            out.append({"id": r["id"], "game": r["game"], "login_type": r["login_type"],
                        "login": r["login"], "email_mask": self._mask_email(r["email"]),
                        "has_password": bool(r["password_enc"]),
                        "has_email_password": bool(r["email_password_enc"]),
                        "imap_host": r["imap_host"] or "", "proxy": r["proxy"] or "",
                        "state": r["state"], "note": r["note"] or "",
                        "tags": json.loads(r["tags"] or "[]")})
        return out

    def mobile_get(self, acc_id: int) -> dict:
        r = self.db.execute("SELECT * FROM mobile_accounts WHERE id=?", (int(acc_id),)).fetchone()
        if not r:
            raise KeyError(f"нет мобильного аккаунта #{acc_id}")
        v = self.vault
        return {"id": r["id"], "game": r["game"], "login_type": r["login_type"], "login": r["login"],
                "password": v.decrypt(r["password_enc"]) or "", "email": r["email"] or "",
                "email_password": v.decrypt(r["email_password_enc"]) or "",
                "imap_host": r["imap_host"] or "", "proxy": r["proxy"] or "", "state": r["state"],
                "note": r["note"] or "", "tags": json.loads(r["tags"] or "[]")}

    def mobile_update(self, acc_id: int, *, game: str, login_type: str, login: str,
                      email: str | None, imap_host: str | None, proxy: str | None,
                      note: str, tags: list[str], password: str | None = None,
                      email_password: str | None = None) -> None:
        v = self.vault
        self.db.execute(
            "UPDATE mobile_accounts SET game=?, login_type=?, login=?, email=?, imap_host=?, "
            "proxy=?, note=?, tags=? WHERE id=?",
            (game, login_type, login, email, imap_host, proxy, note, json.dumps(tags), int(acc_id)))
        if password:
            self.db.execute("UPDATE mobile_accounts SET password_enc=? WHERE id=?",
                            (v.encrypt(password), int(acc_id)))
        if email_password:
            self.db.execute("UPDATE mobile_accounts SET email_password_enc=? WHERE id=?",
                            (v.encrypt(email_password), int(acc_id)))

    def mobile_set_state(self, acc_id: int, state: str, rest_until: float = 0.0) -> None:
        self.db.execute("UPDATE mobile_accounts SET state=?, rest_until=? WHERE id=?",
                        (state, rest_until, int(acc_id)))

    def mobile_delete(self, acc_id: int) -> None:
        self.db.execute("DELETE FROM mobile_accounts WHERE id=?", (int(acc_id),))

    def mobile_claim_free(self, game: str) -> int | None:
        """Атомарно занять свободный аккаунт пула game: вернуть его id и пометить 'rented'."""
        row = self.db.execute(
            "SELECT id FROM mobile_accounts WHERE game=? AND state='free' ORDER BY id LIMIT 1",
            (game,)).fetchone()
        if not row:
            return None
        self.db.execute("UPDATE mobile_accounts SET state='rented' WHERE id=? AND state='free'",
                        (row["id"],))
        chk = self.db.execute("SELECT state FROM mobile_accounts WHERE id=?", (row["id"],)).fetchone()
        return row["id"] if chk and chk["state"] == "rented" else None

    def mobile_free_count(self, game: str) -> int:
        return self.db.execute("SELECT COUNT(*) n FROM mobile_accounts WHERE game=? AND state='free'",
                               (game,)).fetchone()["n"]

    # ---------- лоты PUBG Mobile ----------
    def mobile_lot_set(self, marketplace: str, lot_id: str, game: str, hours: int, enabled: bool) -> None:
        self.db.execute(
            "INSERT INTO mobile_lots (marketplace, lot_id, game, hours, enabled) VALUES (?,?,?,?,?) "
            "ON CONFLICT(marketplace, lot_id) DO UPDATE SET game=excluded.game, hours=excluded.hours, "
            "enabled=excluded.enabled",
            (marketplace, lot_id, game, int(hours), int(enabled)))

    def mobile_lot_list(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM mobile_lots ORDER BY lot_id")
        return [dict(r) | {"enabled": bool(r["enabled"])} for r in rows]

    def mobile_lot_get(self, marketplace: str, lot_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM mobile_lots WHERE marketplace=? AND lot_id=?",
                            (marketplace, lot_id)).fetchone()
        return (dict(r) | {"enabled": bool(r["enabled"])}) if r else None

    def mobile_lot_delete(self, marketplace: str, lot_id: str) -> None:
        self.db.execute("DELETE FROM mobile_lots WHERE marketplace=? AND lot_id=?", (marketplace, lot_id))

    # ---------- аренды PUBG Mobile ----------
    def mobile_rental_add(self, marketplace: str, deal_id: str | None, chat_id: str | None,
                          account_id: int, game: str, login: str, started: float,
                          expires: float, status: str = "active") -> int:
        cur = self.db.execute(
            "INSERT INTO mobile_rentals (marketplace, deal_id, chat_id, account_id, game, login, "
            "started, expires, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (marketplace, deal_id, chat_id, int(account_id), game, login, started, expires, status))
        return cur.lastrowid

    def mobile_rental_by_deal(self, marketplace: str, deal_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM mobile_rentals WHERE marketplace=? AND deal_id=?",
                            (marketplace, deal_id)).fetchone()
        return dict(r) if r else None

    def mobile_rental_by_chat(self, marketplace: str, chat_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM mobile_rentals WHERE marketplace=? AND chat_id=? "
                            "AND status='active' ORDER BY id DESC LIMIT 1",
                            (marketplace, chat_id)).fetchone()
        return dict(r) if r else None

    def mobile_rental_list(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT * FROM mobile_rentals ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def mobile_rental_update_status(self, rental_id: int, status: str) -> None:
        self.db.execute("UPDATE mobile_rentals SET status=? WHERE id=?", (status, int(rental_id)))

    def mobile_rentals_expired(self, now: float) -> list[dict]:
        rows = self.db.execute("SELECT * FROM mobile_rentals WHERE status='active' AND expires<=?", (now,))
        return [dict(r) for r in rows]

    # ---------- CRM покупателей (по Steam-арендам, где известна личность покупателя) ----------
    def buyers_crm(self, marketplace: str | None = None) -> list[dict]:
        """История покупателей: число сделок, суммарная выручка (LTV), первая/последняя сделка.

        Считаем по таблице rentals (в KoSell/PUBG личность покупателя на нашей стороне не
        хранится). Исключаем служебные записи блокировки по ЧС (state='cancelled' при пустом
        account_id); продления (cancelled, но с account_id) и очередь — считаем как сделки."""
        where = "NOT (state='cancelled' AND account_id IS NULL)"
        params: list = []
        if marketplace:
            where += " AND marketplace=?"
            params.append(marketplace)
        rows = self.db.execute(
            "SELECT buyer_id, MAX(buyer_name) AS buyer_name, COUNT(*) AS deals, "
            "COALESCE(SUM(price),0) AS ltv, MIN(started_at) AS first_seen, "
            "MAX(started_at) AS last_seen FROM rentals WHERE " + where +
            " GROUP BY buyer_id ORDER BY ltv DESC, deals DESC", params)
        return [{"buyer_id": r["buyer_id"], "buyer_name": r["buyer_name"],
                 "deals": r["deals"], "ltv": r["ltv"],
                 "first_seen": r["first_seen"], "last_seen": r["last_seen"]} for r in rows]

    # ---------- stats ----------
    def stats(self, since: float) -> dict:
        r = self.db.execute("SELECT COUNT(*) n, COALESCE(SUM(price),0) rev FROM rentals "
                            "WHERE started_at>=? AND state!='cancelled'", (since,)).fetchone()
        per_acc = self.db.execute(
            "SELECT a.login, COUNT(r.id) n, COALESCE(SUM(r.price),0) rev, "
            "COALESCE(SUM(MIN(r.ends_at, ?) - MAX(r.started_at, ?)),0) busy "
            "FROM accounts a LEFT JOIN rentals r ON r.account_id=a.id AND r.ends_at>=? AND r.state!='queued' "
            "GROUP BY a.id ORDER BY rev DESC", (time.time(), since, since)).fetchall()
        span = max(time.time() - since, 1)
        return {
            "rentals": r["n"], "revenue": r["rev"],
            "accounts": [{"login": x["login"], "rentals": x["n"], "revenue": x["rev"],
                          "utilization": round(100 * max(x["busy"], 0) / span, 1)} for x in per_acc],
        }
