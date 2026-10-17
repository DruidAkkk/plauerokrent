"""Проверка обновлений через публичные GitHub Releases (канал раздачи).

Сетевой вызов делает backend; здесь — чистая, тестируемая логика: разбор версии, сравнение
и выбор .exe-ассета из ответа GitHub API. Репозиторий публичный, поэтому токен не нужен.
"""
from __future__ import annotations

import re

from . import __version__

REPO = "DruidAkkk/plauerokrent"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
CURRENT = __version__

_NUM = re.compile(r"\d+")


def parse_version(s: str | None) -> tuple[int, ...]:
    """'v1.2.3' / '1.2.3-beta' → (1, 2, 3). Нецифровые хвосты игнорируем, пустое → (0,)."""
    if not s:
        return (0,)
    nums = _NUM.findall(str(s))
    return tuple(int(x) for x in nums) or (0,)


def is_newer(latest: str | None, current: str | None) -> bool:
    """True, если latest строго новее current (покомпонентное сравнение версий)."""
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def pick_asset(assets: list[dict] | None) -> str | None:
    """Ссылка на установщик из ассетов релиза: предпочитаем *Setup*.exe, иначе любой .exe."""
    exes = [a for a in (assets or [])
            if str(a.get("name", "")).lower().endswith(".exe") and a.get("browser_download_url")]
    if not exes:
        return None
    setup = next((a for a in exes if "setup" in str(a.get("name", "")).lower()), None)
    return (setup or exes[0])["browser_download_url"]


def summarize_release(data: dict, current: str = CURRENT) -> dict:
    """Свести ответ GitHub API releases/latest к тому, что нужно UI."""
    latest = data.get("tag_name") or data.get("name") or ""
    return {
        "current": current,
        "latest": latest,
        "update_available": is_newer(latest, current),
        "url": data.get("html_url") or RELEASES_PAGE,
        "asset_url": pick_asset(data.get("assets")),
        "notes": (data.get("body") or "").strip(),
    }
