"""Agent tools: provider-neutral definitions, enable/disable, timed dispatch, provider formats.

Each tool returns JSON-serializable data. User-fixable problems (unknown product, empty cart)
come back as {"error": ..., "candidates": [...]} instead of raising, so the LLM can ask a
follow-up question rather than the turn failing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.catalog import LookupError_
from backend.tools import pricing
from backend.tools.cart import store


@dataclass
class ToolContext:
    cart_id: str
    retrieve: Callable[..., dict] | None = None  # bound Retriever.retrieve, for search_knowledge


@dataclass(frozen=True)
class Tool:
    name: str
    group: str
    description: str
    parameters: dict
    run: Callable[[ToolContext, dict], Any]
    changes_cart: bool = False


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


PRODUCT = {"type": "string", "description": "Product name as the user said it, e.g. 'CRM' or 'Nimbus Payroll'."}
TIER = {"type": "string", "description": "Tier: Free, Starter, Professional or Enterprise. Omit for the default."}
SEATS = {"type": "integer", "minimum": 1, "description": "Number of users/seats. Default 1."}
BILLING = {"type": "string", "enum": ["monthly", "annual"]}
CATEGORY = {"type": "string", "description": "Optional product family, e.g. 'Finance & Accounting' or 'HR'."}


def _search(ctx: ToolContext, a: dict) -> dict:
    if not ctx.retrieve:
        return {"error": "Knowledge search is not available in this session."}
    r = ctx.retrieve(a["query"], k=a.get("k", 5))
    return {"results": [{"title": h["title"], "text": h["text"]} for h in r["hits"]],
            "retrieval_ms": r["timings_ms"]["total"]}


TOOLS: list[Tool] = [
    Tool("search_knowledge", "Knowledge",
         "Search the Nimbus knowledge base (products, features, plans, FAQs, refund/trial/billing/SLA/"
         "security policies, company info). Use for factual questions; not for cart actions or price math.",
         _obj({"query": {"type": "string", "description": "What to look up, as a standalone question."},
               "k": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Chunks to return (default 5)."}},
              ["query"]),
         _search),
    Tool("add_to_cart", "Cart",
         "Add a product to the user's cart. Defaults: cheapest paid tier, 1 seat, annual billing "
         "(the website's default). Enterprise tiers cannot be added (custom pricing).",
         _obj({"product": PRODUCT, "tier": TIER, "seats": SEATS,
               "billing": {**BILLING, "description": "Billing period. Default annual."}}, ["product"]),
         lambda c, a: store.add(c.cart_id, a["product"], a.get("tier"), a.get("seats", 1), a.get("billing", "annual")),
         changes_cart=True),
    Tool("remove_from_cart", "Cart",
         "Remove one item from the cart, identified by product (and tier if the cart has several tiers of it).",
         _obj({"product": PRODUCT, "tier": TIER}, ["product"]),
         lambda c, a: store.remove(c.cart_id, product=a["product"], tier=a.get("tier")),
         changes_cart=True),
    Tool("clear_cart", "Cart", "Remove every item from the cart. Confirm with the user first.",
         _obj({}), lambda c, a: store.clear(c.cart_id), changes_cart=True),
    Tool("get_cart_total", "Cart",
         "Show cart contents and totals: per month, per year, and due today. Optionally also what it would "
         "cost if everything were billed monthly or annually.",
         _obj({"compare_billing": {**BILLING, "description": "Also price the whole cart this way."}}),
         lambda c, a: pricing.cart_total(store.view(c.cart_id), a.get("compare_billing"))),
    Tool("checkout", "Checkout", "Check out the whole cart (demo: places an order, no payment taken). "
         "Confirm the items and total with the user first.",
         _obj({}), lambda c, a: store.checkout(c.cart_id), changes_cart=True),
    Tool("checkout_item", "Checkout", "Check out a single item from the cart, leaving the rest. Confirm first.",
         _obj({"product": PRODUCT, "tier": TIER}, ["product"]),
         lambda c, a: store.checkout(c.cart_id, product=a["product"], tier=a.get("tier")),
         changes_cart=True),
    Tool("annual_price", "Pricing",
         "Price of a product tier billed monthly vs annually, per seat and for the given seats, per month and per year.",
         _obj({"product": PRODUCT, "tier": TIER, "seats": SEATS}, ["product"]),
         lambda c, a: pricing.annual_price(a["product"], a.get("tier"), a.get("seats", 1))),
    Tool("annual_savings", "Pricing",
         "Percentage and dollar savings from paying annually up front instead of monthly. Give a product "
         "for one plan, or omit it to compute savings for the whole cart.",
         _obj({"product": PRODUCT, "tier": TIER, "seats": SEATS}),
         lambda c, a: pricing.annual_savings(a.get("product"), a.get("tier"), a.get("seats", 1),
                                             cart_lines=store.view(c.cart_id)["lines"])),
    Tool("sort_products_by_price", "Catalog",
         "List products sorted by price, ascending (cheapest first) or descending.",
         _obj({"order": {"type": "string", "enum": ["asc", "desc"]}, "tier": TIER,
               "billing": {**BILLING, "description": "Default monthly."}, "category": CATEGORY}, ["order"]),
         lambda c, a: pricing.sort_products_by_price(a["order"], a.get("tier"), a.get("billing", "monthly"),
                                                     a.get("category"))),
    Tool("top_k_expensive", "Catalog", "The k most expensive products (by their priciest non-custom tier unless a tier is given).",
         _obj({"k": {"type": "integer", "minimum": 1, "maximum": 24, "description": "How many. Default 5."},
               "tier": TIER, "billing": {**BILLING, "description": "Default monthly."}, "category": CATEGORY}),
         lambda c, a: pricing.top_k_expensive(a.get("k", 5), a.get("tier"), a.get("billing", "monthly"),
                                              a.get("category"))),
]
BY_NAME = {t.name: t for t in TOOLS}


def call(name: str, args: dict, ctx: ToolContext) -> dict:
    """Run one tool. Always returns {"name", "args", "result", "ms", "ok", "changes_cart"}."""
    tool = BY_NAME.get(name)
    t0 = time.perf_counter()
    if not tool:
        result, ok = {"error": f"Unknown tool {name!r}."}, False
    else:
        try:
            result, ok = tool.run(ctx, args or {}), True
        except LookupError_ as e:
            result, ok = {"error": str(e), **({"candidates": e.candidates} if e.candidates else {})}, False
        except (TypeError, KeyError) as e:  # bad/missing arguments from the model
            result, ok = {"error": f"Invalid arguments for {name}: {e}"}, False
    return {"name": name, "args": args, "result": result, "ok": ok,
            "changes_cart": bool(tool and tool.changes_cart and ok),
            "ms": round((time.perf_counter() - t0) * 1000, 2)}


def select(enabled: list[str] | None) -> list[Tool]:
    """enabled=None means all tools; [] means none."""
    return list(TOOLS) if enabled is None else [t for t in TOOLS if t.name in set(enabled)]


# ------------------------------------------------------------------ provider formats

def to_openai(tools: list[Tool]) -> list[dict]:
    return [{"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
            for t in tools]


def to_anthropic(tools: list[Tool]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]


def to_gemini(tools: list[Tool]) -> list[dict]:
    def strip(schema: dict) -> dict:  # Gemini's schema subset has no additionalProperties
        s = {k: v for k, v in schema.items() if k != "additionalProperties"}
        if "properties" in s:
            s["properties"] = {k: strip(v) for k, v in s["properties"].items()}
        return s
    return [{"function_declarations": [{"name": t.name, "description": t.description,
                                        "parameters": strip(t.parameters)} for t in tools]}]


def describe() -> list[dict]:
    return [{"name": t.name, "group": t.group, "description": t.description, "parameters": t.parameters,
             "changes_cart": t.changes_cart} for t in TOOLS]
