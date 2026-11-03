"""Выбирает для каждой позиции самого дешёвого поставщика и складывает одинаковые позиции."""

import math

from .models import CatalogItem, OrderRow, Plan, Unmatched


CURRENCY = "₸"  # меняется из настроек (CURRENCY в .env)


def fmt_num(x: float) -> str:
    if math.isclose(x, round(x)):
        return f"{int(round(x)):,}".replace(",", " ")
    return f"{x:,.2f}".replace(",", " ").rstrip("0").rstrip(".")


def fmt_money(x: float | None) -> str:
    return "—" if x is None else f"{fmt_num(round(x, 2))} {CURRENCY}"


def build_plan(lines: list[dict], catalog: list[CatalogItem]) -> Plan:
    by_id = {c.id: c for c in catalog}
    rows: dict[int, OrderRow] = {}
    unmatched: list[Unmatched] = []

    for line in lines:
        requested = line.get("requested", "").strip()
        author = line.get("author", "").strip()
        qty_text = line.get("quantity_text", "").strip()
        comment = line.get("comment", "").strip()

        options: list[tuple[CatalogItem, float]] = []
        for cand in line.get("candidates", []):
            item = by_id.get(cand.get("item_id"))
            qty = cand.get("quantity") or 0
            if item is not None and qty > 0:
                options.append((item, float(qty)))

        if not options:
            unmatched.append(Unmatched(requested, author, qty_text, comment))
            continue

        priced = [o for o in options if o[0].price is not None]
        if priced:
            best_item, best_qty = min(priced, key=lambda o: o[1] * o[0].price)
        else:
            best_item, best_qty = options[0]

        alternatives = []
        seen_suppliers = {best_item.supplier}
        for item, qty in sorted(
            options, key=lambda o: math.inf if o[0].price is None else o[1] * o[0].price
        ):
            if item.supplier in seen_suppliers:
                continue
            seen_suppliers.add(item.supplier)
            total = None if item.price is None else qty * item.price
            alternatives.append(f"{item.supplier}: {item.name} — {fmt_money(total)}")

        row = rows.get(best_item.id)
        if row is None:
            row = rows[best_item.id] = OrderRow(item=best_item, quantity=0)
        row.quantity += best_qty
        row.requested.append(f"{requested} ({qty_text})" if qty_text else requested)
        if author and author not in row.authors:
            row.authors.append(author)
        if comment:
            row.comments.append(comment)
        for alt in alternatives:
            if alt not in row.alternatives:
                row.alternatives.append(alt)

    by_supplier: dict[str, list[OrderRow]] = {}
    for row in rows.values():
        by_supplier.setdefault(row.item.supplier, []).append(row)
    for supplier_rows in by_supplier.values():
        supplier_rows.sort(key=lambda r: r.item.name.casefold())
    by_supplier = dict(sorted(by_supplier.items(), key=lambda kv: kv[0].casefold()))
    return Plan(by_supplier=by_supplier, unmatched=unmatched)


def supplier_messages(plan: Plan) -> list[str]:
    """Тексты, которые удобно переслать поставщику (по одному на поставщика)."""
    texts = []
    for supplier, rows in plan.by_supplier.items():
        lines = [f"📦 {supplier}"]
        for r in rows:
            unit = f" {r.item.unit}" if r.item.unit else ""
            lines.append(f"• {r.item.name} — {fmt_num(r.quantity)}{unit}")
        lines.append(f"Итого ≈ {fmt_money(plan.supplier_total(supplier))}")
        texts.append("\n".join(lines))
    if plan.unmatched:
        lines = ["❓ Не нашёл в прайсах:"]
        for u in plan.unmatched:
            qty = f" — {u.quantity_text}" if u.quantity_text else ""
            who = f" ({u.author})" if u.author else ""
            lines.append(f"• {u.requested}{qty}{who}")
        texts.append("\n".join(lines))
    return texts
