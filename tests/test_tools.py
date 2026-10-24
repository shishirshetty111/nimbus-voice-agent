import pytest

import backend.tools.registry as reg
from backend.catalog import LookupError_, find_product, find_tier
from backend.tools import pricing
from backend.tools.cart import CartStore


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "store", CartStore(tmp_path / "carts.json"))
    return reg.ToolContext(cart_id="test-cart-1")


def call(ctx, name, **args):
    return reg.call(name, args, ctx)


# ---------------------------------------------------------------- lookup

@pytest.mark.parametrize("spoken,expected", [
    ("crm", "Nimbus CRM"), ("Nimbus CRM", "Nimbus CRM"), ("nimbus-crm", "Nimbus CRM"),
    ("nimbs crm", "Nimbus CRM"), ("the payroll app", "Nimbus Payroll"), ("board", "Nimbus Boards"),
])
def test_find_product_forgiving(spoken, expected):
    assert find_product(spoken)["name"] == expected


def test_find_product_unknown():
    with pytest.raises(LookupError_):
        find_product("spaceship")


def test_tier_prefix_and_default():
    crm = find_product("crm")
    assert find_tier(crm, "pro")["name"] == "Professional"
    assert find_tier(crm, None)["name"] == "Starter"  # cheapest paid tier, like the site's button


# ---------------------------------------------------------------- cart

def test_add_merges_and_totals(ctx):
    call(ctx, "add_to_cart", product="crm", tier="professional", seats=3)            # annual $36
    call(ctx, "add_to_cart", product="crm", tier="professional", seats=2)            # merges -> 5 seats
    out = call(ctx, "add_to_cart", product="payroll", tier="starter", billing="monthly")  # monthly $20
    lines, t = out["result"]["lines"], out["result"]["totals"]
    assert [l["seats"] for l in lines] == [5, 1]
    assert t["per_month"] == 5 * 36 + 20
    assert t["due_today"] == 5 * 36 * 12 + 20  # annual lines are paid a year up front
    assert out["changes_cart"]


def test_enterprise_rejected(ctx):
    out = call(ctx, "add_to_cart", product="crm", tier="enterprise")
    assert not out["ok"] and "custom" in out["result"]["error"]
    assert not out["changes_cart"]


def test_remove_clear_checkout(ctx):
    call(ctx, "add_to_cart", product="crm")
    call(ctx, "add_to_cart", product="docs")
    call(ctx, "add_to_cart", product="vault")
    assert call(ctx, "remove_from_cart", product="docs")["result"]["totals"]["items"] == 2
    order = call(ctx, "checkout_item", product="vault")["result"]
    assert order["order"]["order_id"].startswith("NB-") and order["totals"]["items"] == 1
    assert call(ctx, "checkout")["result"]["totals"]["items"] == 0
    assert not call(ctx, "checkout")["ok"]  # empty cart
    call(ctx, "add_to_cart", product="crm")
    assert call(ctx, "clear_cart")["result"]["removed_items"] == 1


def test_remove_missing_item_lists_cart(ctx):
    call(ctx, "add_to_cart", product="crm")
    out = call(ctx, "remove_from_cart", product="docs")
    assert not out["ok"] and out["result"]["candidates"] == ["Nimbus CRM Starter"]


def test_carts_are_isolated(ctx):
    call(ctx, "add_to_cart", product="crm")
    other = reg.ToolContext(cart_id="test-cart-2")
    assert reg.call("get_cart_total", {}, other)["result"]["totals"]["items"] == 0


# ---------------------------------------------------------------- pricing

def test_annual_savings_product_and_rounding_exception():
    assert pricing.annual_savings("crm", "starter")["saved_percent"] == 20.0
    assert pricing.annual_savings("docs", "starter")["saved_percent"] == 25.0


def test_annual_savings_for_cart(ctx):
    call(ctx, "add_to_cart", product="crm", tier="professional", seats=2)
    r = call(ctx, "annual_savings")["result"]
    assert r["scope"] == "cart" and r["yearly_if_paid_monthly"] == 45 * 2 * 12 and r["saved_percent"] == 20.0


def test_annual_price_seats():
    r = pricing.annual_price("vault", "professional", seats=10)
    assert r["monthly_billing"]["per_year"] == 3000 and r["annual_billing"]["per_year_paid_upfront"] == 2400


def test_sorting_and_top_k():
    asc = pricing.sort_products_by_price("asc")["products"]
    prices = [p["per_seat_per_month"] for p in asc]
    assert prices == sorted(prices) and len(asc) == 24
    top = pricing.top_k_expensive(3)["products"]
    assert len(top) == 3 and top[0]["per_seat_per_month"] >= top[-1]["per_seat_per_month"]
    hr = pricing.sort_products_by_price("desc", category="hr")["products"]
    assert hr and all(p["family"] == "HR & People" for p in hr)


# ---------------------------------------------------------------- registry

def test_select_all_none_subset():
    assert len(reg.select(None)) == len(reg.TOOLS) == 11
    assert reg.select([]) == []
    assert [t.name for t in reg.select(["checkout", "add_to_cart"])] == ["add_to_cart", "checkout"]


def test_provider_formats():
    tools = reg.select(None)
    assert reg.to_openai(tools)[0]["type"] == "function"
    assert "input_schema" in reg.to_anthropic(tools)[0]
    decl = reg.to_gemini(tools)[0]["function_declarations"][1]
    assert "additionalProperties" not in decl["parameters"]


def test_unknown_tool_and_bad_args(ctx):
    assert not call(ctx, "fly_to_moon")["ok"]
    assert "Invalid arguments" in call(ctx, "add_to_cart")["result"]["error"]  # missing product
