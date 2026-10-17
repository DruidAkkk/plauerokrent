"""Тесты логики проверки обновлений (без сети)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot import updater


def test_parse_version():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.0.0") == (1, 0, 0)
    assert updater.parse_version("v2.0.0-beta1") == (2, 0, 0, 1)
    assert updater.parse_version("") == (0,)
    assert updater.parse_version(None) == (0,)
    print("PASS test_parse_version")


def test_is_newer():
    assert updater.is_newer("v1.0.1", "1.0.0") is True
    assert updater.is_newer("v2.0.0", "1.9.9") is True
    assert updater.is_newer("1.0.0", "1.0.0") is False
    assert updater.is_newer("1.0.0", "1.0.1") is False
    # разная длина: 1.1 новее 1.0.9, 1.0 не новее 1.0.0
    assert updater.is_newer("1.1", "1.0.9") is True
    assert updater.is_newer("1.0", "1.0.0") is False
    print("PASS test_is_newer")


def test_pick_asset_prefers_setup():
    assets = [
        {"name": "rentbot.zip", "browser_download_url": "u0"},
        {"name": "rentbot-portable.exe", "browser_download_url": "u1"},
        {"name": "rentbot-Setup.exe", "browser_download_url": "u2"},
    ]
    assert updater.pick_asset(assets) == "u2"
    assert updater.pick_asset([{"name": "only.exe", "browser_download_url": "u9"}]) == "u9"
    assert updater.pick_asset([{"name": "notes.txt", "browser_download_url": "x"}]) is None
    assert updater.pick_asset([]) is None
    assert updater.pick_asset(None) is None
    print("PASS test_pick_asset_prefers_setup")


def test_summarize_release():
    data = {
        "tag_name": "v1.4.0",
        "html_url": "https://github.com/DruidAkkk/plauerokrent/releases/tag/v1.4.0",
        "body": "  Новые плагины  ",
        "assets": [{"name": "rentbot-Setup.exe", "browser_download_url": "https://x/Setup.exe"}],
    }
    r = updater.summarize_release(data, current="1.0.0")
    assert r["update_available"] is True
    assert r["latest"] == "v1.4.0"
    assert r["asset_url"] == "https://x/Setup.exe"
    assert r["notes"] == "Новые плагины"
    # та же версия — апдейта нет
    assert updater.summarize_release({"tag_name": "1.0.0"}, current="1.0.0")["update_available"] is False
    print("PASS test_summarize_release")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in tests:
        f()
    print(f"{len(tests)}/{len(tests)} passed")
