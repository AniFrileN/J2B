import asyncio
import io
import json
from types import SimpleNamespace

import openpyxl
import pytest

from zakup_bot.ai import Claude
from zakup_bot.bot import _pack
from zakup_bot.db import DB
from zakup_bot.excel import build_workbook
from zakup_bot.files import UnsupportedFile, price_chunks, text_chunks
from zakup_bot.models import CatalogItem, PendingMessage
from zakup_bot.planner import build_plan, fmt_money, supplier_messages

CATALOG = [
    CatalogItem(1, "Метро", "Молоко 3,2% 1 л", "шт", 95.0),
    CatalogItem(2, "Молочка", "Молоко 3,2% 5 л", "шт", 420.0),
    CatalogItem(3, "Овощебаза", "Лимон", "кг", 180.0),
    CatalogItem(4, "Метро", "Лимон", "кг", 210.0),
    CatalogItem(5, "Метро", "Сливки 33% 1 л", "шт", None),
]


def test_cheapest_supplier_wins_and_duplicates_merge():
    lines = [
        {"requested": "молоко 10 л", "author": "Иван", "quantity_text": "10 л",
         "candidates": [{"item_id": 1, "quantity": 10}, {"item_id": 2, "quantity": 2}], "comment": ""},
        {"requested": "лимоны", "author": "Оля", "quantity_text": "3 кг",
         "candidates": [{"item_id": 4, "quantity": 3}, {"item_id": 3, "quantity": 3}], "comment": ""},
        {"requested": "лимон", "author": "Иван", "quantity_text": "2 кг",
         "candidates": [{"item_id": 3, "quantity": 2}], "comment": ""},
        {"requested": "сливки", "author": "Оля", "quantity_text": "",
         "candidates": [{"item_id": 5, "quantity": 1}], "comment": "количество не указано"},
        {"requested": "трюфель", "author": "Шеф", "quantity_text": "100 г",
         "candidates": [{"item_id": 999, "quantity": 1}], "comment": ""},
    ]
    plan = build_plan(lines, CATALOG)

    # 2 x 420 = 840 дешевле, чем 10 x 95 = 950
    molochka = plan.by_supplier["Молочка"]
    assert molochka[0].item.id == 2 and molochka[0].quantity == 2
    assert any("Метро" in a for a in molochka[0].alternatives)

    lemons = plan.by_supplier["Овощебаза"][0]
    assert lemons.quantity == 5
    assert lemons.authors == ["Оля", "Иван"]

    metro = plan.by_supplier["Метро"]
    assert [r.item.id for r in metro] == [5]
    assert metro[0].total is None

    assert [u.requested for u in plan.unmatched] == ["трюфель"]
    assert plan.grand_total == 840 + 900
    texts = supplier_messages(plan)
    assert texts[-1].startswith("❓")
    assert any("Лимон — 5 кг" in t for t in texts)


def test_excel_has_sheet_per_supplier():
    lines = [
        {"requested": "молоко", "author": "Иван", "quantity_text": "5 л",
         "candidates": [{"item_id": 2, "quantity": 1}], "comment": ""},
        {"requested": "икра", "author": "Шеф", "quantity_text": "", "candidates": [], "comment": ""},
    ]
    data = build_workbook(build_plan(lines, CATALOG), "Заказ")
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Сводка", "Молочка", "Не найдено"]
    assert wb["Молочка"]["A4"].value == "Молоко 3,2% 5 л"
    assert wb["Не найдено"]["A2"].value == "икра"


def test_xlsx_and_csv_chunks():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Наименование", "Ед", "Цена"])
    for i in range(600):
        ws.append([f"Товар {i}", "кг", 10.0 + i])
    buf = io.BytesIO()
    wb.save(buf)
    chunks = price_chunks("price.xlsx", buf.getvalue())
    assert len(chunks) == 3
    assert "Наименование" in chunks[1].text  # заголовок повторяется
    assert "Товар 0\tкг\t10" in chunks[0].text

    csv_chunks = price_chunks("p.csv", "Товар;Цена\nЛук;40\n".encode("cp1251"))
    assert "Лук\t40" in csv_chunks[0].text

    assert price_chunks("p.pdf", b"%PDF-1.4")[0].block["type"] == "document"
    assert price_chunks("p.png", b"x")[0].block["type"] == "image"
    assert text_chunks("а\n\nб")[0].text == "а\nб"
    with pytest.raises(UnsupportedFile):
        price_chunks("p.docx", b"x")


def test_db_roundtrip(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    db.add_items("Метро", [{"name": "Лук", "unit": "кг", "price": 40}], replace=True)
    assert db.canonical_supplier("  метро ") == "Метро"
    db.add_items("Метро", [{"name": "Морковь", "unit": "кг", "price": 35}], replace=False)
    assert db.suppliers() == [("Метро", 2)]
    db.add_items("Метро", [{"name": "Свёкла", "unit": "кг", "price": None}], replace=True)
    assert [c.name for c in db.catalog()] == ["Свёкла"]

    db.save_pending(1, 10, "Иван", "лук 5 кг")
    db.save_pending(1, 11, "Оля", "морковь")
    db.update_pending(1, 10, "лук 7 кг")
    assert [m.text for m in db.pending(1)] == ["лук 7 кг", "морковь"]
    db.delete_pending(1, [10])
    assert [m.message_id for m in db.pending(1)] == [11]
    db.delete_pending(1)
    assert db.pending(1) == []


class FakeStream:
    def __init__(self, payload, calls, kwargs):
        self.payload = payload
        calls.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=json.dumps(self.payload))],
        )


def fake_client(payload, calls):
    return SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream(payload, calls, kw)))
    )


def test_claude_requests():
    calls = []
    claude = Claude("claude-opus-5-5", fake_client({"items": [
        {"name": " Лук  репчатый ", "unit": "кг", "price": 40},
        {"name": "", "unit": "", "price": None},
    ]}, calls))
    items = asyncio.run(claude.extract_prices(text_chunks("Лук репчатый кг 40"), "p.txt"))
    assert items == [{"name": "Лук репчатый", "unit": "кг", "price": 40.0}]
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["type"] == "json_schema"

    calls.clear()
    claude = Claude("claude-opus-5-5", fake_client({"lines": []}, calls))
    lines = asyncio.run(claude.match_order(CATALOG, [PendingMessage(1, "Иван", "молоко 10 л")]))
    assert lines == []
    text = calls[0]["messages"][0]["content"][0]["text"]
    assert "1 | Метро | Молоко 3,2% 1 л | шт | 95" in text
    assert "[Иван]\nмолоко 10 л" in text


def test_pack_and_money():
    parts = _pack(["a" * 3000, "b" * 3000, "c" * 9000])
    assert all(len(p) <= 4000 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts).count("c") == 9000
    assert fmt_money(1234.5) == "1 234.5 ₽"
    assert fmt_money(None) == "—"


def test_plural():
    from zakup_bot.bot import plural

    assert plural(1, "позиция", "позиции", "позиций") == "1 позиция"
    assert plural(2, "позиция", "позиции", "позиций") == "2 позиции"
    assert plural(11, "позиция", "позиции", "позиций") == "11 позиций"
    assert plural(25, "позиция", "позиции", "позиций") == "25 позиций"
