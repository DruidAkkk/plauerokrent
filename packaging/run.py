"""Точка входа установленной сборки rentbot (PyInstaller-фриз).

Отдельный скрипт, чтобы PyInstaller анализировал приложение как пакет `app`.
"""
import sys

from app.main import main

if __name__ == "__main__":
    sys.exit(main())
