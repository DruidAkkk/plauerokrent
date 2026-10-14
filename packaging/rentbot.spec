# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спецификация rentbot (Windows, onedir, оконное приложение).

Собирать из корня проекта:
    .venv/Scripts/pyinstaller packaging/rentbot.spec --noconfirm

Вкладывает веб-ресурсы (app/web -> web) и Node-сайдкар (sidecar -> sidecar).
Пути в рантайме разрешаются через sys._MEIPASS (см. app/main.py, rentbot/steam/sidecar.py).
"""
import os
from PyInstaller.utils.hooks import collect_submodules, collect_all

ROOT = os.path.abspath(os.getcwd())

datas = [(os.path.join(ROOT, "app", "web"), "web"),
         (os.path.join(ROOT, "sidecar"), "sidecar")]

# Портативный node.exe — чтобы Steam-сайдкар работал у клиента без установки Node.
_node = os.path.join(ROOT, "packaging", "node", "node.exe")
if os.path.isfile(_node):
    datas.append((_node, "node"))
binaries = []
hiddenimports = []

# Все подмодули приложения (многие импортируются лениво внутри функций)
for pkg in ("rentbot", "app"):
    hiddenimports += collect_submodules(pkg)

# curl_cffi тащит бинарный libcurl — собираем полностью
_d, _b, _h = collect_all("curl_cffi")
datas += _d; binaries += _b; hiddenimports += _h

# прочие явные зависимости (webview/cryptography покрываются хуками PyInstaller)
hiddenimports += ["clr", "bs4", "cryptography", "curl_cffi"]

icon = os.path.join(ROOT, "packaging", "rentbot.ico")
icon = icon if os.path.isfile(icon) else None

a = Analysis(
    [os.path.join(ROOT, "packaging", "run.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="rentbot",
    console=False,            # оконное приложение, без консоли
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="rentbot")
