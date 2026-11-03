import json
import sqlite3
import time

from .models import CatalogItem, PendingMessage

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    supplier TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    price REAL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_supplier ON items(supplier);
CREATE TABLE IF NOT EXISTS pending (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

ORDER_TTL = 60 * 24 * 3600  # готовые заказы храним 60 дней


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    # --- прайсы ---

    def canonical_supplier(self, name: str) -> str:
        """Возвращает уже существующее написание поставщика (без учёта регистра)."""
        name = " ".join(name.split())
        for (existing,) in self.conn.execute("SELECT DISTINCT supplier FROM items"):
            if existing.casefold() == name.casefold():
                return existing
        return name

    def add_items(self, supplier: str, items: list[dict], replace: bool) -> int:
        now = time.time()
        with self.conn:
            if replace:
                self.conn.execute("DELETE FROM items WHERE supplier = ?", (supplier,))
            self.conn.executemany(
                "INSERT INTO items (supplier, name, unit, price, updated_at) VALUES (?, ?, ?, ?, ?)",
                [(supplier, i["name"], i.get("unit") or "", i.get("price"), now) for i in items],
            )
        return len(items)

    def delete_supplier(self, supplier: str) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM items WHERE supplier = ?", (supplier,))
        return cur.rowcount

    def suppliers(self) -> list[tuple[str, int]]:
        return list(
            self.conn.execute(
                "SELECT supplier, COUNT(*) FROM items GROUP BY supplier ORDER BY supplier"
            )
        )

    def catalog(self) -> list[CatalogItem]:
        rows = self.conn.execute(
            "SELECT id, supplier, name, unit, price FROM items ORDER BY supplier, name"
        )
        return [CatalogItem(*r) for r in rows]

    # --- готовые заказы ---

    def save_order(self, chat_id: int, title: str, data: dict) -> int:
        now = time.time()
        with self.conn:
            self.conn.execute("DELETE FROM orders WHERE created_at < ?", (now - ORDER_TTL,))
            cur = self.conn.execute(
                "INSERT INTO orders (chat_id, title, data, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, title, json.dumps(data, ensure_ascii=False), now),
            )
        return cur.lastrowid

    def get_order(self, order_id: int) -> tuple[int, str, dict] | None:
        row = self.conn.execute(
            "SELECT chat_id, title, data FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None:
            return None
        return row[0], row[1], json.loads(row[2])

    # --- собранные списки ---

    def save_pending(self, chat_id: int, message_id: int, author: str, text: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO pending (chat_id, message_id, author, text, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (chat_id, message_id) DO UPDATE SET text = excluded.text",
                (chat_id, message_id, author, text, time.time()),
            )

    def update_pending(self, chat_id: int, message_id: int, text: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE pending SET text = ? WHERE chat_id = ? AND message_id = ?",
                (text, chat_id, message_id),
            )

    def pending(self, chat_id: int) -> list[PendingMessage]:
        rows = self.conn.execute(
            "SELECT message_id, author, text FROM pending WHERE chat_id = ? ORDER BY created_at",
            (chat_id,),
        )
        return [PendingMessage(*r) for r in rows]

    def delete_pending(self, chat_id: int, message_ids: list[int] | None = None) -> None:
        with self.conn:
            if message_ids is None:
                self.conn.execute("DELETE FROM pending WHERE chat_id = ?", (chat_id,))
            else:
                self.conn.executemany(
                    "DELETE FROM pending WHERE chat_id = ? AND message_id = ?",
                    [(chat_id, m) for m in message_ids],
                )
