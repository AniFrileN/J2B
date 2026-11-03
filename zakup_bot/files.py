"""Превращает присланный прайс (Excel, CSV, PDF, фото, текст) в куски для Claude."""

import csv
import io
from dataclasses import dataclass

ROWS_PER_CHUNK = 250

IMAGE_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


class UnsupportedFile(Exception):
    pass


@dataclass
class PriceChunk:
    """Либо текст (таблица построчно), либо файл целиком (PDF/картинка)."""

    text: str | None = None
    data: bytes | None = None
    mime: str | None = None


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return " ".join(str(value).split())


def _chunk_rows(rows: list[str], header: str = "") -> list[PriceChunk]:
    chunks = []
    for start in range(0, len(rows), ROWS_PER_CHUNK):
        part = rows[start : start + ROWS_PER_CHUNK]
        prefix = header + "\n" if header and start else ""
        chunks.append(PriceChunk(text=prefix + "\n".join(part)))
    return chunks


def _table_to_chunks(sheets: list[tuple[str, list[list]]]) -> list[PriceChunk]:
    chunks: list[PriceChunk] = []
    for sheet_name, raw_rows in sheets:
        rows = []
        for raw in raw_rows:
            cells = [_cell(v) for v in raw]
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                rows.append("\t".join(cells))
        if not rows:
            continue
        # Первая строка листа часто заголовок — повторяем её в каждом куске.
        header = f"Лист «{sheet_name}»\n{rows[0]}"
        first = [f"Лист «{sheet_name}»"] + rows
        chunks.extend(_chunk_rows(first, header))
    return chunks


def _xlsx(data: bytes) -> list[PriceChunk]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets = [(ws.title, list(ws.iter_rows(values_only=True))) for ws in wb.worksheets]
    wb.close()
    return _table_to_chunks(sheets)


def _xls(data: bytes) -> list[PriceChunk]:
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    sheets = [
        (sh.name, [sh.row_values(i) for i in range(sh.nrows)]) for sh in book.sheets()
    ]
    return _table_to_chunks(sheets)


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _csv(data: bytes) -> list[PriceChunk]:
    text = decode_text(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error:
        rows = [line.split(";") for line in text.splitlines()]
    return _table_to_chunks([("CSV", rows)])


def text_chunks(text: str) -> list[PriceChunk]:
    lines = [line for line in text.splitlines() if line.strip()]
    return _chunk_rows(lines)


def price_chunks(filename: str, data: bytes, mime: str | None = None) -> list[PriceChunk]:
    ext = _ext(filename)
    if ext in ("xlsx", "xlsm"):
        return _xlsx(data)
    if ext == "xls":
        return _xls(data)
    if ext == "csv":
        return _csv(data)
    if ext == "txt":
        return text_chunks(decode_text(data))
    if ext == "pdf" or mime == "application/pdf":
        return [PriceChunk(data=data, mime="application/pdf")]
    media = IMAGE_TYPES.get(ext) or (mime if mime in IMAGE_TYPES.values() else None)
    if media:
        return [PriceChunk(data=data, mime=media)]
    raise UnsupportedFile(
        f"Не умею читать файлы .{ext or '?'}. Пришлите Excel, CSV, PDF, фото или текст."
    )
