"""Поддельные реализации портов для тестов и локального прогона без сети."""
from __future__ import annotations

from ..ports import Marketplace, Notifier, SteamBackend
from ..steam.guard import generate_code


class FakeMarket(Marketplace):
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.files: list[tuple[str, str, str]] = []
        self.lot_active: dict[str, bool] = {}
        self.bumped: list[tuple[str, float]] = []

    async def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def set_lot_active(self, lot_id: str, active: bool) -> None:
        self.lot_active[lot_id] = active

    async def send_file(self, chat_id: str, file_path: str, caption: str = "") -> None:
        self.files.append((chat_id, file_path, caption))

    async def bump_item(self, lot_id: str, price: float) -> None:
        self.bumped.append((lot_id, price))

    def last(self, chat_id: str) -> str | None:
        for cid, text in reversed(self.sent):
            if cid == chat_id:
                return text
        return None


class FakeSteam(SteamBackend):
    def __init__(self, fail: bool = False, deauth_fail: bool = False):
        self.fail = fail
        self.deauth_fail = deauth_fail
        self.rotations = 0
        self.deauths: list[str] = []  # логины, по которым сделан авто-кик
        self._counter = 0

    def guard_code(self, shared_secret: str) -> str:
        return generate_code(shared_secret)

    async def rotate_credentials(self, *, login, password, shared_secret, identity_secret, proxy):
        self.rotations += 1
        if self.fail:
            raise RuntimeError("steam rotate failed (fake)")
        self._counter += 1
        return f"newpwd{self._counter}"

    async def deauth(self, *, login, password, shared_secret, proxy):
        self.deauths.append(login)
        if self.deauth_fail:
            return {"ok": False, "message": "deauth failed (fake)"}
        return {"ok": True, "supported": True, "total": 2, "sessions": [], "message": "ok (fake)"}


class FakeNotifier(Notifier):
    def __init__(self):
        self.messages: list[str] = []

    async def notify(self, text: str) -> None:
        self.messages.append(text)
