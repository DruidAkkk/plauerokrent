"""Чтение одноразового кода входа (OTP) с подконтрольной почты мобильного аккаунта по IMAP.

PUBG Mobile / соц-логины при входе с нового устройства шлют код на email — почтой владеет
продавец, бот читает свежий код и пересылает покупателю. Эвристика: берём самые свежие письма,
ищем 4–8-значный код в письмах, похожих на проверочные (тема/отправитель про code/verification/
Facebook/Google/PUBG). Только stdlib (imaplib/email). Пароль почты НЕ логируем.
"""
from __future__ import annotations

import email as emaillib
import imaplib
import re
from email.header import decode_header

# Домен → IMAP-хост (если не задан явно).
IMAP_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "mail.ru": "imap.mail.ru",
    "bk.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru",
    "list.ru": "imap.mail.ru",
    "yandex.ru": "imap.yandex.com",
    "yandex.com": "imap.yandex.com",
    "ya.ru": "imap.yandex.com",
    "icloud.com": "imap.mail.me.com",
    "rambler.ru": "imap.rambler.ru",
    "proton.me": "imap.protonmail.ch",
}

CODE_RE = re.compile(r"\b(\d{4,8})\b")
HINT_RE = re.compile(r"(code|verif|confirm|одноразов|код|подтвержд|security|login|sign)", re.I)
SENDER_HINT = re.compile(r"(facebook|google|pubg|tencent|level ?infinite|midasbuy|proxima|krafton|twitter|\bx\b|apple)", re.I)


def host_for(email_addr: str, override: str | None = None) -> str | None:
    if override:
        return override
    if "@" not in (email_addr or ""):
        return None
    return IMAP_HOSTS.get(email_addr.split("@", 1)[1].lower())


def _decode(s) -> str:
    if not s:
        return ""
    out = []
    for part, enc in decode_header(s):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", "ignore"))
        else:
            out.append(part)
    return "".join(out)


def _body_text(msg) -> str:
    chunks = []
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain":
                try:
                    chunks.append(p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "ignore"))
                except Exception:  # noqa: BLE001
                    pass
    else:
        try:
            chunks.append(msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(chunks)


def read_otp(email_addr: str, password: str, imap_host: str | None = None,
             lookback: int = 15, timeout: int = 15) -> tuple[str | None, str]:
    """Вернуть (код, сообщение). code=None, если не нашли/ошибка; сообщение — для журнала."""
    host = host_for(email_addr, imap_host)
    if not host:
        return None, "неизвестен IMAP-хост почты — укажите его в настройках аккаунта"
    if not password:
        return None, "не задан пароль почты (нужен пароль приложения IMAP)"
    try:
        M = imaplib.IMAP4_SSL(host, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return None, f"не подключиться к {host}: {type(e).__name__}"
    try:
        try:
            M.login(email_addr, password)
        except imaplib.IMAP4.error:
            return None, "почта отклонила вход (нужен пароль приложения / включён IMAP?)"
        M.select("INBOX")
        typ, data = M.search(None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return None, "входящих писем нет"
        ids = data[0].split()[-lookback:]
        best = None
        for i in reversed(ids):  # от свежих к старым
            typ, md = M.fetch(i, "(RFC822)")
            if typ != "OK" or not md or not md[0]:
                continue
            msg = emaillib.message_from_bytes(md[0][1])
            subject = _decode(msg.get("Subject"))
            sender = _decode(msg.get("From"))
            text = subject + "\n" + _body_text(msg)
            hint = HINT_RE.search(text) or SENDER_HINT.search(sender)
            m = CODE_RE.search(text)
            if m and hint:
                return m.group(1), f"код из письма «{subject[:40]}»"
            if m and best is None:
                best = m.group(1)  # запасной: любой код, если явных подсказок не нашли
        if best:
            return best, "код найден (без явной подсказки — проверьте, что верный)"
        return None, "в последних письмах код не найден"
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass
