"""Запуск десктоп-панели rentbot (pywebview).

    python -m app.main            # БД: rentbot.db рядом с проектом
    python -m app.main --db путь  # своя БД

Окно открывает локальный HTML (app/web) и общается с Python через js_api (app.backend.Backend).
Никакого сетевого сервера — всё локально, как и положено self-hosted инструменту.
"""
from __future__ import annotations

import argparse
import os
import sys

import webview

from app.backend import Backend


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _web_dir() -> str:
    """Папка веб-ресурсов: из бандла PyInstaller (sys._MEIPASS) или рядом с исходником."""
    if _is_frozen():
        return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(__file__)), "web")
    return os.path.join(os.path.dirname(__file__), "web")


def _default_db() -> str:
    """БД: в установленной сборке — пользовательская папка (Program Files только для чтения),
    в dev-режиме — рядом с проектом (прежнее поведение)."""
    if _is_frozen():
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "rentbot")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "rentbot.db")
    return os.path.join(os.getcwd(), "rentbot.db")


WEB_DIR = _web_dir()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=_default_db())
    ap.add_argument("--debug", action="store_true", help="открыть DevTools")
    args = ap.parse_args()

    backend = Backend(args.db)
    webview.create_window(
        "rentbot — панель владельца",
        url=os.path.join(WEB_DIR, "index.html"),
        js_api=backend,
        width=1180, height=760, min_size=(920, 600),
        background_color="#0e1117",
    )
    # gui=None → авто (на Windows EdgeChromium/WebView2)
    webview.start(debug=args.debug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
