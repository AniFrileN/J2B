from dataclasses import dataclass, field


@dataclass
class CatalogItem:
    id: int
    supplier: str
    name: str
    unit: str
    price: float | None


@dataclass
class PendingMessage:
    message_id: int
    author: str
    text: str


@dataclass
class OrderRow:
    """Одна строка заказа у поставщика (одинаковые позиции от разных людей сложены)."""

    item: CatalogItem
    quantity: float
    requested: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)

    @property
    def total(self) -> float | None:
        if self.item.price is None:
            return None
        return self.quantity * self.item.price


@dataclass
class Unmatched:
    requested: str
    author: str
    quantity_text: str
    comment: str


@dataclass
class Plan:
    by_supplier: dict[str, list[OrderRow]]
    unmatched: list[Unmatched]

    def supplier_total(self, supplier: str) -> float:
        return sum(r.total or 0 for r in self.by_supplier.get(supplier, []))

    @property
    def grand_total(self) -> float:
        return sum(self.supplier_total(s) for s in self.by_supplier)
