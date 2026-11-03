"""Итоговая Excel-таблица: сводка, отдельный лист на каждого поставщика и «Не найдено»."""

import io
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .models import Plan

MONEY = '#,##0.00 "₽"'
HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
SUPPLIER_FILL = PatternFill("solid", fgColor="FCE4D6")
TOTAL_FILL = PatternFill("solid", fgColor="E2EFDA")
BOLD = Font(bold=True)
THIN = Border(bottom=Side(style="thin", color="BFBFBF"))
WRAP = Alignment(wrap_text=True, vertical="top")

FULL_COLUMNS = [
    ("Товар", 42),
    ("Кол-во", 9),
    ("Ед.", 7),
    ("Цена", 12),
    ("Сумма", 13),
    ("Что писали", 32),
    ("Кто", 18),
    ("Другие поставщики", 40),
    ("Комментарий", 30),
]
SHORT_COLUMNS = FULL_COLUMNS[:5]


def _sheet_title(name: str, used: set[str]) -> str:
    title = re.sub(r"[\[\]:*?/\\]", " ", name).strip()[:31] or "Поставщик"
    base, n = title, 2
    while title.casefold() in used:
        suffix = f" ({n})"
        title = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(title.casefold())
    return title


def _header(ws, row: int, columns) -> None:
    for col, (title, width) in enumerate(columns, start=1):
        c = ws.cell(row=row, column=col, value=title)
        c.font = BOLD
        c.fill = HEADER_FILL
        ws.column_dimensions[get_column_letter(col)].width = width


def _write_rows(ws, row: int, rows, full: bool) -> int:
    for r in rows:
        values = [r.item.name, r.quantity, r.item.unit, r.item.price, r.total]
        if full:
            values += [
                "; ".join(r.requested),
                ", ".join(r.authors),
                "\n".join(r.alternatives),
                "; ".join(r.comments),
            ]
        for col, v in enumerate(values, start=1):
            c = ws.cell(row=row, column=col, value=v)
            c.alignment = WRAP
            c.border = THIN
            if col in (4, 5):
                c.number_format = MONEY
        row += 1
    return row


def _total(ws, row: int, label: str, amount: float, width: int) -> None:
    ws.cell(row=row, column=1, value=label).font = BOLD
    c = ws.cell(row=row, column=5, value=amount)
    c.font = BOLD
    c.number_format = MONEY
    for col in range(1, width + 1):
        ws.cell(row=row, column=col).fill = TOTAL_FILL


def build_workbook(plan: Plan, title: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=14)
    row = 3
    width = len(FULL_COLUMNS)
    for supplier, rows in plan.by_supplier.items():
        c = ws.cell(row=row, column=1, value=supplier)
        c.font = Font(bold=True, size=12)
        for col in range(1, width + 1):
            ws.cell(row=row, column=col).fill = SUPPLIER_FILL
        row += 1
        _header(ws, row, FULL_COLUMNS)
        row = _write_rows(ws, row + 1, rows, full=True)
        _total(ws, row, f"Итого {supplier}", plan.supplier_total(supplier), width)
        row += 2
    if plan.by_supplier:
        _total(ws, row, "ИТОГО по всем поставщикам", plan.grand_total, width)
        row += 2
    if plan.unmatched:
        ws.cell(row=row, column=1, value=f"Не найдено в прайсах: {len(plan.unmatched)} поз. — см. лист «Не найдено»").font = Font(bold=True, color="C00000")

    used = {"сводка", "не найдено"}
    for supplier, rows in plan.by_supplier.items():
        sh = wb.create_sheet(_sheet_title(supplier, used))
        sh.cell(row=1, column=1, value=f"Заказ: {supplier}").font = Font(bold=True, size=14)
        _header(sh, 3, SHORT_COLUMNS)
        end = _write_rows(sh, 4, rows, full=False)
        _total(sh, end, "Итого", plan.supplier_total(supplier), len(SHORT_COLUMNS))

    if plan.unmatched:
        sh = wb.create_sheet("Не найдено")
        cols = [("Что писали", 40), ("Кол-во", 14), ("Кто", 18), ("Комментарий", 40)]
        _header(sh, 1, cols)
        for i, u in enumerate(plan.unmatched, start=2):
            for col, v in enumerate([u.requested, u.quantity_text, u.author, u.comment], start=1):
                sh.cell(row=i, column=col, value=v).alignment = WRAP

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
