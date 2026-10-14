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

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.getcwd(), "rentbot.db"))
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
