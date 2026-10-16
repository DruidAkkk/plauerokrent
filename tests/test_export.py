"""Тесты экспорта продаж в CSV (Steam / KoSell / PUBG в одной таблице)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rentbot import export
from rentbot.domain import Rental, RentalState
from rentbot.storage import Storage
from rentbot.vault import Vault


def _storage():
    dbp = os.path.join(tempfile.mkdtemp(), "r.db")
    return Storage(dbp, Vault("pw", Vault.new_salt()))


def _seed(s):
    # Steam-аренда: завершённая продажа (с покупателем и ценой)
    s.insert_rental(Rental(None, "playerok", "D1", "LOT1", "B1", "Вася", "C1", None,
                           RentalState.ENDED, 3000.0, 4000.0, 150.0))
    # служебный CANCELLED-маркер (продление/ЧС) — НЕ продажа, в экспорт не идёт
    s.insert_rental(Rental(None, "playerok", "DX", "LOT1", "B9", "Скам", "CX", None,
                           RentalState.CANCELLED, 3500.0, 3500.0, 0.0))
    # KoSell
    s.kosell_rental_add("UID-1", "playerok", "D2", "C2", 77, "Raft", "login", 24,
                        "", "", "active", 2000.0)
    # PUBG Mobile
    s.mobile_add(game="pubg_mobile", login_type="facebook", login="acc@mail.com",
                 password="p", email=None, email_password=None, imap_host=None,
                 proxy=None, note="", tags=[])
    acc_id = s.mobile_list()[0]["id"]
    s.mobile_rental_add("playerok", "D3", "C3", acc_id, "pubg_mobile", "acc@mail.com",
                        1000.0, 5000.0, "expired")


def test_rows_cover_all_plugins_and_skip_cancelled():
    s = _storage()
    _seed(s)
    rows = export.sales_rows(s)
    plugins = [r["plugin"] for r in rows]
    assert plugins == ["Steam-аренда", "KoSell", "PUBG Mobile"], f"сортировка/состав: {plugins}"
    assert all(r["deal"] != "DX" for r in rows), "CANCELLED-маркер не должен попадать в экспорт"
    steam = rows[0]
    assert steam["buyer"] == "Вася" and steam["price"] == 150.0
    print("PASS test_rows_cover_all_plugins_and_skip_cancelled")


def test_csv_has_bom_header_and_delimiter():
    s = _storage()
    _seed(s)
    csv = export.sales_csv(s)
    assert csv.startswith("﻿"), "нет BOM — Excel сломает кириллицу"
    lines = csv.replace("﻿", "").rstrip("\r\n").split("\r\n")
    assert lines[0] == "Дата;Плагин;Товар;Заказ;Покупатель;Цена;Статус"
    assert len(lines) == 1 + 3, "шапка + 3 продажи"
    assert "Steam-аренда;" in lines[1] and lines[1].endswith(";завершена")
    print("PASS test_csv_has_bom_header_and_delimiter")


def test_cell_escaping_and_price():
    assert export._cell("Вася;Босс") == '"Вася;Босс"'
    assert export._cell('кавычка"тут') == '"кавычка""тут"'
    assert export._cell("просто") == "просто"
    assert export._price(150.0) == "150"
    assert export._price(99.5) == "99.50"
    assert export._price(None) == ""
    print("PASS test_cell_escaping_and_price")


def test_empty_storage_no_rows():
    s = _storage()
    assert export.sales_rows(s) == []
    csv = export.sales_csv(s)
    assert csv.replace("﻿", "").strip() == "Дата;Плагин;Товар;Заказ;Покупатель;Цена;Статус"
    print("PASS test_empty_storage_no_rows")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in tests:
        f()
    print(f"{len(tests)}/{len(tests)} passed")
