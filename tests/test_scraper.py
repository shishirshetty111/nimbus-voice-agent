import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))

import scrape  # noqa: E402

CATALOG = json.loads((ROOT / "data" / "catalog.json").read_text(encoding="utf-8"))


def test_annual_math():
    m = scrape.annual_math({"priceMonthly": 15, "priceAnnualMonthly": 12})
    assert (m["year_if_monthly"], m["year_if_annual"], m["saved"], m["saved_pct"]) == (180, 144, 36, 20.0)
    assert scrape.annual_math({"priceMonthly": 0, "priceAnnualMonthly": 0}) is None
    assert scrape.annual_math({"priceMonthly": None, "priceAnnualMonthly": None, "custom": True}) is None


def test_money():
    assert scrape.money(15) == "$15"
    assert scrape.money(1234) == "$1,234"
    assert scrape.money(12.5) == "$12.50"


def test_every_doc_built_and_in_context():
    docs = scrape.build_docs(CATALOG)
    assert set(docs) == {name for name, _ in scrape.DOC_ORDER}
    context = scrape.build_context(docs)
    for p in CATALOG["products"]:
        assert p["name"] in context
    for text in CATALOG["policies"].values():
        assert text in context


def test_every_faq_and_tier_is_a_chunk():
    chunks = scrape.build_chunks(CATALOG)
    ids = {c["id"] for c in chunks}
    for p in CATALOG["products"]:
        for i in range(1, len(p["faqs"]) + 1):
            assert f"{p['id']}:faq:{i}" in ids
        for t in p["tiers"]:
            assert f"{p['id']}:tier:{scrape.slug(t['name'])}" in ids
    for key in CATALOG["policies"]:
        assert f"policy:{key}" in ids


def test_chunks_carry_metadata_and_title():
    for c in scrape.build_chunks(CATALOG):
        assert c["text"].startswith(c["title"])
        if c["type"] in {"product_overview", "product_features", "tier", "addon", "faq"}:
            assert c["product_id"] and c["category_id"]


def test_enterprise_is_custom_not_zero():
    docs = scrape.build_docs(CATALOG)
    assert "| Enterprise | Custom | Custom |" in docs["pricing.md"]
    assert "Nimbus CRM | Enterprise" not in docs["monthly-vs-annual.md"]


def test_savings_exceptions_named():
    text = scrape.build_docs(CATALOG)["monthly-vs-annual.md"]
    assert "Nimbus Docs Starter (25%)" in text
