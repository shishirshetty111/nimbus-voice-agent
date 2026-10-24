"""Typed access to data/catalog.json with forgiving product/tier lookup for spoken input."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from functools import lru_cache

from backend.config import ROOT

CATALOG_PATH = ROOT / "data" / "catalog.json"


class LookupError_(ValueError):
    """A product or tier the user named could not be resolved. `candidates` helps the LLM ask back."""

    def __init__(self, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def products() -> list[dict]:
    return catalog()["products"]


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    s = re.sub(r"\bnimbus\b", " ", s)  # "Nimbus CRM" == "CRM" == "the crm"
    return " ".join(w for w in s.split() if w not in {"the", "app", "product", "plan"})


def find_product(name: str) -> dict:
    """Resolve 'crm', 'Nimbus CRM', 'nimbus-crm', 'nimbs crm' to a product."""
    q = _norm(name)
    if not q:
        raise LookupError_("No product name given.")
    by_key = {}
    for p in products():
        by_key[_norm(p["name"])] = p
        by_key[_norm(p["id"].replace("-", " "))] = p
    if q in by_key:
        return by_key[q]
    contains = {id(p): p for k, p in by_key.items() if q in k.split() or k in q.split()}
    if len(contains) == 1:
        return next(iter(contains.values()))
    close = difflib.get_close_matches(q, list(by_key), n=3, cutoff=0.6)
    hits = list({by_key[k]["name"]: by_key[k] for k in close}.values())
    if len(hits) == 1 or (hits and difflib.SequenceMatcher(None, q, _norm(hits[0]["name"])).ratio() > 0.85):
        return hits[0]
    names = [p["name"] for p in (hits or contains.values())]
    raise LookupError_(f"No product matches {name!r}." + (f" Did you mean: {', '.join(names)}?" if names else ""), names)


def find_tier(product: dict, tier: str | None) -> dict:
    """Default (no tier named): the cheapest paid tier, matching the site's Add to cart button."""
    tiers = product["tiers"]
    if not tier:
        paid = [t for t in tiers if t.get("priceMonthly")]
        return min(paid, key=lambda t: t["priceMonthly"]) if paid else tiers[0]
    q = re.sub(r"\s*(tier|plan)$", "", tier.lower().strip())
    for t in tiers:
        if t["name"].lower() == q:
            return t
    prefixed = [t for t in tiers if t["name"].lower().startswith(q)]  # "pro" -> Professional
    if len(prefixed) == 1:
        return prefixed[0]
    close = difflib.get_close_matches(q, [t["name"].lower() for t in tiers], n=1, cutoff=0.6)
    if close:
        return next(t for t in tiers if t["name"].lower() == close[0])
    raise LookupError_(f"{product['name']} has no {tier!r} tier. Tiers: {', '.join(t['name'] for t in tiers)}.",
                       [t["name"] for t in tiers])


def is_custom(tier: dict) -> bool:
    return bool(tier.get("custom")) or tier.get("priceMonthly") is None


@dataclass(frozen=True)
class Price:
    monthly: float          # per seat per month, billed monthly
    annual_monthly: float   # per seat per month, billed annually


def tier_price(tier: dict) -> Price:
    if is_custom(tier):
        raise LookupError_("Enterprise pricing is custom. Contact sales at sales@nimbus.example.")
    return Price(float(tier["priceMonthly"]), float(tier["priceAnnualMonthly"]))
