from dataclasses import asdict, dataclass, field


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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        by_supplier = {
            supplier: [
                OrderRow(**{**row, "item": CatalogItem(**row["item"])}) for row in rows
            ]
            for supplier, rows in data["by_supplier"].items()
        }
        return cls(by_supplier, [Unmatched(**u) for u in data["unmatched"]])
