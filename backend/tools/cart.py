"""Server-side carts, shared by the website drawer, the voice agent's tools and (later) phone calls.

A cart is keyed by an id the browser keeps in localStorage and sends as X-Cart-Id (a phone call
will use the verified caller's number). Locally carts persist to .runtime/carts.json so restarts keep
them; in production (Upstash Redis configured, see backend/kv.py) each cart is one Redis key, so every
server instance sees the same cart.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from copy import deepcopy

from backend.catalog import find_product, find_tier, is_custom, tier_price, LookupError_
from backend import kv as kv_store
from backend.config import RUNTIME_DIR

STORE_PATH = RUNTIME_DIR / ".runtime" / "carts.json"
BILLING = ("monthly", "annual")
CART_TTL_S = 30 * 24 * 3600  # Redis: an untouched cart expires after 30 days


def _money(x: float) -> float:
    return round(x, 2)


def totals(lines: list[dict]) -> dict:
    """Monthly-equivalent total, what is charged today, and the cost per year."""
    per_month = sum(l["unit_price"] * l["seats"] for l in lines)
    due_today = sum(l["unit_price"] * l["seats"] * (12 if l["billing"] == "annual" else 1) for l in lines)
    return {
        "items": len(lines),
        "seats": sum(l["seats"] for l in lines),
        "per_month": _money(per_month),
        "per_year": _money(per_month * 12),
        "due_today": _money(due_today),
        "currency": "USD",
    }


class CartStore:
    def __init__(self, path=STORE_PATH, kv=None):
        self.path, self.kv = path, kv
        self._lock = threading.Lock()
        self._data = {"carts": {}, "orders": []}
        if kv is None and path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass  # corrupt file: start empty rather than refuse to serve

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # Storage: every method reads a cart with _lines() and writes it back with _put().
    def _lines(self, cart_id: str) -> list[dict]:
        if self.kv:
            return self.kv.get_json(f"cart:{cart_id}") or []
        return self._data["carts"].setdefault(cart_id, [])

    def _put(self, cart_id: str, lines: list[dict]) -> None:
        if self.kv:
            self.kv.set_json(f"cart:{cart_id}", lines, CART_TTL_S)
        else:
            self._data["carts"][cart_id] = lines
            self._save()

    def _add_order(self, order: dict) -> None:
        if self.kv:
            self.kv.push_json(f"orders:{order['cart_id']}", order, CART_TTL_S)
        else:
            self._data["orders"].append(order)  # saved by the _put() that follows

    def view(self, cart_id: str) -> dict:
        with self._lock:
            lines = deepcopy(self._lines(cart_id))
        return {"cart_id": cart_id, "lines": lines, "totals": totals(lines)}

    # -------------------------------------------------------------- mutations

    def add(self, cart_id: str, product: str, tier: str | None = None, seats: int = 1,
            billing: str = "annual") -> dict:
        if billing not in BILLING:
            raise LookupError_(f"billing must be 'monthly' or 'annual', not {billing!r}")
        if seats < 1:
            raise LookupError_("seats must be at least 1")
        p = find_product(product)
        t = find_tier(p, tier)
        if is_custom(t):
            raise LookupError_(f"{p['name']} {t['name']} is custom-priced and cannot be added to the cart. "
                               "Contact sales at sales@nimbus.example.")
        price = tier_price(t)
        unit = price.annual_monthly if billing == "annual" else price.monthly
        with self._lock:
            lines = self._lines(cart_id)
            same = next((l for l in lines if l["product_id"] == p["id"] and l["tier"] == t["name"]
                         and l["billing"] == billing), None)
            if same:
                same["seats"] += seats
            else:
                lines.append({"line_id": secrets.token_hex(4), "product_id": p["id"], "product_name": p["name"],
                              "tier": t["name"], "seats": seats, "billing": billing, "unit_price": unit})
            self._put(cart_id, lines)
        return self.view(cart_id)

    def _match(self, lines: list[dict], line_id: str | None, product: str | None, tier: str | None) -> list[dict]:
        if line_id:
            found = [l for l in lines if l["line_id"] == line_id]
        else:
            if not product:
                raise LookupError_("Say which product to remove.")
            p = find_product(product)
            found = [l for l in lines if l["product_id"] == p["id"]]
            if tier:
                t = find_tier(p, tier)
                found = [l for l in found if l["tier"] == t["name"]]
        if not found:
            raise LookupError_("That item is not in the cart.", [f"{l['product_name']} {l['tier']}" for l in lines])
        if len(found) > 1:
            raise LookupError_("More than one matching item; say which tier.",
                               [f"{l['product_name']} {l['tier']} ({l['billing']})" for l in found])
        return found

    def remove(self, cart_id: str, line_id: str | None = None, product: str | None = None,
               tier: str | None = None) -> dict:
        with self._lock:
            lines = self._lines(cart_id)
            (line,) = self._match(lines, line_id, product, tier)
            lines.remove(line)
            self._put(cart_id, lines)
        return {"removed": line, **self.view(cart_id)}

    def clear(self, cart_id: str) -> dict:
        with self._lock:
            n = len(self._lines(cart_id))
            self._put(cart_id, [])
        return {"removed_items": n, **self.view(cart_id)}

    def checkout(self, cart_id: str, line_id: str | None = None, product: str | None = None,
                 tier: str | None = None) -> dict:
        """Dummy checkout: records an order and removes the purchased lines. No payment is taken."""
        with self._lock:
            lines = self._lines(cart_id)
            if not lines:
                raise LookupError_("The cart is empty.")
            single = bool(line_id or product)
            bought = self._match(lines, line_id, product, tier) if single else list(lines)
            for l in bought:
                lines.remove(l)
            order = {"order_id": "NB-" + secrets.token_hex(3).upper(), "cart_id": cart_id,
                     "created": int(time.time()), "lines": bought, "totals": totals(bought)}
            self._add_order(order)
            self._put(cart_id, lines)
        return {"order": order, "note": "Demo checkout: no payment was taken.", **self.view(cart_id)}


store = CartStore(kv=kv_store.client())
