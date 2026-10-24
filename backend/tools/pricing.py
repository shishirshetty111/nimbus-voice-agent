"""Price math and catalog ranking. Deterministic: the LLM calls these instead of doing arithmetic."""

from __future__ import annotations

import difflib

from backend.catalog import LookupError_, catalog, find_product, find_tier, is_custom, products, tier_price
from backend.tools.cart import totals


def _r(x: float) -> float:
    return round(x, 2)


def annual_price(product: str, tier: str | None = None, seats: int = 1) -> dict:
    p = find_product(product)
    t = find_tier(p, tier)
    price = tier_price(t)
    return {
        "product": p["name"], "tier": t["name"], "seats": seats,
        "monthly_billing": {"per_seat_per_month": price.monthly, "per_month": _r(price.monthly * seats),
                            "per_year": _r(price.monthly * seats * 12)},
        "annual_billing": {"per_seat_per_month": price.annual_monthly,
                           "per_year_paid_upfront": _r(price.annual_monthly * seats * 12)},
    }


def _savings(year_monthly: float, year_annual: float) -> dict:
    saved = year_monthly - year_annual
    return {"yearly_if_paid_monthly": _r(year_monthly), "yearly_if_paid_annually": _r(year_annual),
            "saved_per_year": _r(saved), "saved_percent": round(saved / year_monthly * 100, 1) if year_monthly else 0.0}


def annual_savings(product: str | None = None, tier: str | None = None, seats: int = 1,
                   cart_lines: list[dict] | None = None) -> dict:
    """Savings from paying a year up front vs month by month, for one product or the whole cart."""
    if product:
        p = find_product(product)
        t = find_tier(p, tier)
        price = tier_price(t)
        return {"product": p["name"], "tier": t["name"], "seats": seats,
                **_savings(price.monthly * seats * 12, price.annual_monthly * seats * 12)}
    if not cart_lines:
        raise LookupError_("Name a product, or add items to the cart first.")
    year_monthly = year_annual = 0.0
    for line in cart_lines:
        price = tier_price(find_tier(find_product(line["product_id"]), line["tier"]))
        year_monthly += price.monthly * line["seats"] * 12
        year_annual += price.annual_monthly * line["seats"] * 12
    return {"scope": "cart", "items": len(cart_lines), **_savings(year_monthly, year_annual)}


def cart_total(cart: dict, billing: str | None = None) -> dict:
    """Current cart totals; with billing=monthly|annual also shows what everything would cost that way."""
    out = {"lines": cart["lines"], "totals": cart["totals"]}
    if billing in ("monthly", "annual") and cart["lines"]:
        repriced = []
        for line in cart["lines"]:
            price = tier_price(find_tier(find_product(line["product_id"]), line["tier"]))
            unit = price.annual_monthly if billing == "annual" else price.monthly
            repriced.append({**line, "billing": billing, "unit_price": unit})
        out[f"if_all_{billing}"] = totals(repriced)
    return out


# ------------------------------------------------------------------ ranking

def _category_id(category: str | None) -> str | None:
    if not category:
        return None
    cats = catalog()["categories"]
    names = {c["name"].lower(): c["id"] for c in cats} | {c["id"]: c["id"] for c in cats}
    q = category.lower().strip()
    hit = names.get(q) or next((cid for n, cid in names.items() if q in n), None)
    if not hit:
        close = difflib.get_close_matches(q, list(names), n=1, cutoff=0.5)
        hit = names[close[0]] if close else None
    if not hit:
        raise LookupError_(f"No product family matches {category!r}.", [c["name"] for c in cats])
    return hit


def _priced(tier: str | None, billing: str, category: str | None, pick: str) -> list[dict]:
    """One row per product. tier=None picks the product's cheapest ('min') or priciest ('max') paid tier."""
    cid = _category_id(category)
    rows = []
    for p in products():
        if cid and p["categoryId"] != cid:
            continue
        paid = [t for t in p["tiers"] if not is_custom(t) and t.get("priceMonthly")]
        if tier:
            paid = [t for t in paid if t["name"].lower().startswith(tier.lower())]
        if not paid:
            continue
        key = "priceAnnualMonthly" if billing == "annual" else "priceMonthly"
        t = (min if pick == "min" else max)(paid, key=lambda t: t[key])
        rows.append({"product": p["name"], "family": p["category"], "tier": t["name"],
                     "per_seat_per_month": t[key], "billing": billing})
    return rows


def sort_products_by_price(order: str = "asc", tier: str | None = None, billing: str = "monthly",
                           category: str | None = None) -> dict:
    if order not in ("asc", "desc"):
        raise LookupError_("order must be 'asc' or 'desc'")
    rows = _priced(tier, billing, category, pick="min")
    rows.sort(key=lambda r: (r["per_seat_per_month"], r["product"]), reverse=order == "desc")
    basis = f"{tier} tier" if tier else "starting (cheapest paid) tier"
    return {"order": order, "basis": f"{basis}, {billing} billing, per seat per month", "products": rows}


def top_k_expensive(k: int = 5, tier: str | None = None, billing: str = "monthly",
                    category: str | None = None) -> dict:
    rows = _priced(tier, billing, category, pick="max")
    rows.sort(key=lambda r: (-r["per_seat_per_month"], r["product"]))
    basis = f"{tier} tier" if tier else "highest-priced non-custom tier"
    return {"k": k, "basis": f"{basis}, {billing} billing, per seat per month",
            "note": "Enterprise tiers are custom-priced and excluded.", "products": rows[:max(1, k)]}
