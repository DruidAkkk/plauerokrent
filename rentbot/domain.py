"""Доменные модели: аккаунты, аренды, привязки лотов."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AccountState(str, Enum):
    FREE = "free"              # можно выдавать
    RENTED = "rented"          # в аренде
    ROTATING = "rotating"      # меняем пароль / выкидываем сессии
    RESTING = "resting"        # «отдых» между арендами (антибан, антифрод Faceit)
    QUARANTINE = "quarantine"  # что-то пошло не так — нужен человек
    PAUSED = "paused"          # выключен владельцем


class RentalState(str, Enum):
    QUEUED = "queued"      # оплачено, но свободного аккаунта нет
    ACTIVE = "active"
    ENDED = "ended"        # время вышло, аккаунт отправлен на ротацию
    CANCELLED = "cancelled"


class RotationMode(str, Enum):
    AUTO = "auto"      # бот сам меняет пароль (Steam)
    SEMI = "semi"      # бот готовит пароль/коды, человек жмёт кнопку
    MANUAL = "manual"  # только напоминание человеку


@dataclass
class Account:
    id: int | None
    game: str                     # "cs2", "dota2", "faceit", ... (метка)
    login: str
    password: str                 # хранится зашифрованным (см. vault.py)
    shared_secret: str | None = None    # из maFile, для !код
    identity_secret: str | None = None  # из maFile, для подтверждений
    mafile_json: str | None = None      # целиком, зашифрованным
    proxy: str | None = None
    state: AccountState = AccountState.FREE
    rest_until: float = 0.0
    note: str = ""
    tags: list[str] = field(default_factory=list)  # "faceit_verified", "prime", ...


@dataclass
class LotBinding:
    """Лот на площадке → пул аккаунтов и параметры аренды."""
    marketplace: str          # "playerok"
    lot_id: str
    game: str                 # какой пул обслуживает лот
    hours: float              # срок аренды за одну покупку
    rotation: RotationMode = RotationMode.AUTO
    title: str = ""


@dataclass
class Rental:
    id: int | None
    marketplace: str
    deal_id: str              # id сделки на площадке — ключ идемпотентности
    lot_id: str
    buyer_id: str
    buyer_name: str
    chat_id: str
    account_id: int | None
    state: RentalState
    started_at: float
    ends_at: float
    price: float = 0.0
    warned: bool = False      # предупреждение «осталось 15 минут» уже отправлено
    codes_issued: int = 0
