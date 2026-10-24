"""Extract the Nimbus knowledge base from data/catalog.json.

Writes, into knowledge/:
  company.md, families.md, products.md, pricing.md, monthly-vs-annual.md,
  faq.md, refund.md, terms.md   -- one clean document per topic
  context.md                    -- all of the above in one file (RAG-less mode)
  chunks.jsonl                  -- semantic chunks with metadata (FAISS RAG mode)

Deterministic and idempotent: same catalog in, byte-identical files out.
Stdlib only; uses tiktoken for token counts when it is installed.

    python scraper/scrape.py [--catalog data/catalog.json] [--out knowledge]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Order of documents inside context.md.
DOC_ORDER = [
    ("company.md", "Company"),
    ("families.md", "Product families"),
    ("products.md", "Products"),
    ("pricing.md", "Pricing"),
    ("monthly-vs-annual.md", "Monthly vs annual billing"),
    ("refund.md", "Refunds, free trial and cancellation"),
    ("terms.md", "Terms and policies"),
    ("faq.md", "Frequently asked questions"),
]


# ---------------------------------------------------------------- helpers

def money(x: float) -> str:
    """$15, $1,234, $12.50"""
    if x is None:
        return "custom"
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def pct(x: float) -> str:
    return f"{x:.0f}%" if float(x).is_integer() else f"{x:.1f}%"


def slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")


def is_paid(tier: dict) -> bool:
    return bool(tier.get("priceMonthly"))


def tier_price_label(tier: dict) -> str:
    if tier.get("custom") or tier.get("priceMonthly") is None:
        return "Custom pricing (contact sales)"
    if tier["priceMonthly"] == 0:
        return "Free"
    return (f"{money(tier['priceMonthly'])} {tier['unit']} billed monthly, "
            f"or {money(tier['priceAnnualMonthly'])} {tier['unit']} billed annually")


def annual_math(tier: dict) -> dict | None:
    """Yearly cost both ways for one seat. None for free/custom tiers."""
    if not is_paid(tier) or tier.get("priceAnnualMonthly") is None:
        return None
    m, a = tier["priceMonthly"], tier["priceAnnualMonthly"]
    monthly_year, annual_year = m * 12, a * 12
    saved = monthly_year - annual_year
    return {
        "monthly": m,
        "annual_monthly": a,
        "year_if_monthly": monthly_year,
        "year_if_annual": annual_year,
        "saved": saved,
        "saved_pct": round(saved / monthly_year * 100, 1),
    }


def starting_price(product: dict) -> str:
    paid = [t for t in product["tiers"] if is_paid(t)]
    free = any(t.get("priceMonthly") == 0 for t in product["tiers"])
    if not paid:
        return "Free" if free else "Custom pricing"
    low = min(paid, key=lambda t: t["priceMonthly"])
    s = f"from {money(low['priceMonthly'])} {low['unit']}"
    return f"Free tier available; paid plans {s}" if free else s.capitalize()


def kv_lines(d: dict) -> list[str]:
    return [f"- **{k}:** {v}" for k, v in d.items()]


def by_category(catalog: dict) -> list[tuple[dict, list[dict]]]:
    return [(cat, [p for p in catalog["products"] if p["categoryId"] == cat["id"]])
            for cat in catalog["categories"]]


# ---------------------------------------------------------------- documents

def doc_company(c: dict) -> str:
    co = c["company"]
    out = [f"# {co['name']}", "",
           f"*{co['tagline']}*", "",
           f"- **Legal name:** {co['legalName']}",
           f"- **Founded:** {co['founded']}",
           f"- **Headquarters:** {co['hq']}", "",
           "## About", "", co["about"], "",
           "## Mission", "", co["mission"], "",
           "## Key facts", "", *kv_lines(co["stats"]), "",
           "## Contact", "", *kv_lines({k.capitalize(): v for k, v in co["contact"].items()}), ""]
    return "\n".join(out)


def doc_families(c: dict) -> str:
    out = ["# Product families", "",
           f"Nimbus offers {len(c['products'])} products in {len(c['categories'])} families (categories).", ""]
    for cat, prods in by_category(c):
        out += [f"## {cat['name']}", "", f"{len(prods)} products:", ""]
        out += [f"- **{p['name']}** — {p['tagline']} ({starting_price(p)})" for p in prods]
        out.append("")
    return "\n".join(out)


def doc_products(c: dict) -> str:
    out = ["# Products", ""]
    for p in c["products"]:
        out += [f"## {p['name']}", "",
                f"*{p['tagline']}*", "",
                f"- **Family:** {p['category']}",
                f"- **Price:** {starting_price(p)}", "",
                p["summary"], "", p["description"], "",
                "### Key features", "", *[f"- {f}" for f in p["keyFeatures"]], "",
                "### Specifications", "", *kv_lines(p["specs"]), "",
                "### Integrations", "", ", ".join(p["integrations"]), "",
                "### Plans", ""]
        for t in p["tiers"]:
            out.append(f"- **{t['name']}** — {tier_price_label(t)}. "
                       f"Includes: {', '.join(t['highlights'])}."
                       + (f" Limits: {', '.join(f'{k} {v}' for k, v in t['limits'].items())}."
                          if t.get("limits") else ""))
        if p.get("addOns"):
            out += ["", "### Add-ons", ""]
            out += [f"- **{a['name']}** ({a['price']}) — {a['desc']}" for a in p["addOns"]]
        out.append("")
    return "\n".join(out)


def doc_pricing(c: dict) -> str:
    out = ["# Pricing", "",
           "All prices are in USD, per user per month. \"Annual\" is the per-month price when billed "
           "annually up front. Enterprise tiers are custom-priced; contact sales.", "",
           c["policies"]["billing"], ""]
    for cat, prods in by_category(c):
        out += [f"## {cat['name']}", ""]
        for p in prods:
            out += [f"### {p['name']}", "",
                    "| Tier | Monthly billing | Annual billing (per month) | Limits |",
                    "|---|---|---|---|"]
            for t in p["tiers"]:
                if t.get("custom") or t.get("priceMonthly") is None:
                    m = a = "Custom"
                else:
                    m, a = money(t["priceMonthly"]), money(t["priceAnnualMonthly"])
                limits = "; ".join(f"{k}: {v}" for k, v in (t.get("limits") or {}).items()) or "—"
                out.append(f"| {t['name']} | {m} | {a} | {limits} |")
            if p.get("addOns"):
                out += ["", "Add-ons: " + "; ".join(f"{a['name']} {a['price']}" for a in p["addOns"])]
            out.append("")
    return "\n".join(out)


def doc_monthly_vs_annual(c: dict) -> str:
    rows, pcts = [], {}
    for p in c["products"]:
        for t in p["tiers"]:
            m = annual_math(t)
            if m:
                pcts[f"{p['name']} {t['name']}"] = m["saved_pct"]
                rows.append(f"| {p['name']} | {t['name']} | {money(m['monthly'])} | {money(m['annual_monthly'])} "
                            f"| {money(m['year_if_monthly'])} | {money(m['year_if_annual'])} "
                            f"| {money(m['saved'])} | {pct(m['saved_pct'])} |")
    # The billing policy advertises one discount, but rounded prices make a few tiers differ.
    # Name the exceptions so the agent quotes the real per-tier figure.
    common = max(set(pcts.values()), key=list(pcts.values()).count)
    odd = {k: v for k, v in pcts.items() if v != common}
    summary = f"Annual billing saves {pct(common)} on every paid tier"
    summary += ("." if not odd else
                " except " + ", ".join(f"{k} ({pct(v)})" for k, v in odd.items())
                + ", where the annual price is rounded. Always quote the per-tier figure from the table.")
    return "\n".join([
        "# Monthly vs annual billing", "",
        c["policies"]["billing"], "",
        "## How it works", "",
        "- **Monthly billing:** pay the monthly price each month; cancel any time and the account stays "
        "active until the end of the current cycle.",
        "- **Annual billing:** pay for 12 months up front at a discounted per-month price.",
        "- Both monthly and annual plans are covered by the 30-day money-back guarantee.",
        "- Free tiers cost nothing either way; Enterprise tiers are custom-priced.", "",
        f"**{summary}**", "",
        "## Savings per seat, per year", "",
        "Figures are for one user. Multiply by the number of seats.", "",
        "| Product | Tier | Monthly price | Annual price (per month) | Year, billed monthly "
        "| Year, billed annually | You save | Savings % |",
        "|---|---|---|---|---|---|---|---|",
        *rows, ""])


def doc_faq(c: dict) -> str:
    out = ["# Frequently asked questions", ""]
    for p in c["products"]:
        out += [f"## {p['name']}", ""]
        for f in p["faqs"]:
            out += [f"**Q: {f['q']}**", "", f"A: {f['a']}", ""]
    return "\n".join(out)


def doc_refund(c: dict) -> str:
    pol = c["policies"]
    return "\n".join(["# Refunds, free trial and cancellation", "",
                      "## Refund policy (30-day money-back guarantee)", "", pol["refund"], "",
                      "## Free trial", "", pol["freeTrial"], "",
                      "## Cancellation", "", pol["cancellation"], ""])


TERMS_SECTIONS = [("billing", "Billing and payment"), ("cancellation", "Cancellation and data retention"),
                  ("refund", "Refunds"), ("sla", "Service level agreement (SLA)"),
                  ("security", "Security and compliance"), ("dataResidency", "Data residency"),
                  ("support", "Support")]


def doc_terms(c: dict) -> str:
    pol = c["policies"]
    out = ["# Terms and policies", "",
           "> Nimbus does not publish a separate Terms & Conditions document. These terms are the "
           "policies published on the Nimbus support page.", ""]
    for key, title in TERMS_SECTIONS:
        out += [f"## {title}", "", pol[key], ""]
    return "\n".join(out)


def build_docs(c: dict) -> dict[str, str]:
    return {
        "company.md": doc_company(c),
        "families.md": doc_families(c),
        "products.md": doc_products(c),
        "pricing.md": doc_pricing(c),
        "monthly-vs-annual.md": doc_monthly_vs_annual(c),
        "refund.md": doc_refund(c),
        "terms.md": doc_terms(c),
        "faq.md": doc_faq(c),
    }


def build_context(docs: dict[str, str]) -> str:
    """One file for the RAG-less mode: TOC + every doc with headings demoted one level."""
    out = ["# Nimbus knowledge base", "",
           "Complete reference for the Nimbus product catalog, pricing and policies.", "",
           "## Contents", ""]
    out += [f"{i}. {title}" for i, (_, title) in enumerate(DOC_ORDER, 1)]
    out.append("")
    for name, _ in DOC_ORDER:
        body = docs[name]
        demoted = "\n".join("#" + line if line.startswith("#") else line for line in body.splitlines())
        out += [demoted, ""]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- chunks

def build_chunks(c: dict) -> list[dict]:
    """Semantic chunks. Each text is prefixed with its product/section so it embeds standalone."""
    chunks: list[dict] = []

    def add(id_, type_, title, text, product=None, category_id=None):
        chunks.append({
            "id": id_, "type": type_, "title": title,
            "product_id": product["id"] if product else None,
            "product_name": product["name"] if product else None,
            "category_id": category_id or (product["categoryId"] if product else None),
            "text": f"{title}\n{text}".strip(),
        })

    co = c["company"]
    add("company:profile", "company", f"About {co['name']}",
        f"{co['legalName']}, founded {co['founded']}, headquartered in {co['hq']}. {co['tagline']} "
        f"{co['about']} Mission: {co['mission']} "
        + " ".join(f"{k}: {v}." for k, v in co["stats"].items()))
    add("company:contact", "company", f"Contact {co['name']}: email addresses and phone number",
        " ".join(f"{k.capitalize()} {'phone number' if k == 'phone' else 'email address'}: {v}."
                 for k, v in co["contact"].items()))

    for key, title in TERMS_SECTIONS + [("freeTrial", "Free trial")]:
        add(f"policy:{key}", "policy", f"Nimbus policy: {title}", c["policies"][key])

    for cat, prods in by_category(c):
        add(f"category:{cat['id']}", "category", f"{cat['name']} product family",
            f"The {cat['name']} family contains {len(prods)} products: "
            + "; ".join(f"{p['name']} — {p['tagline']} ({starting_price(p)})" for p in prods) + ".",
            category_id=cat["id"])

    for p in c["products"]:
        n = p["name"]
        add(f"{p['id']}:overview", "product_overview", f"{n} overview",
            f"Family: {p['category']}. {p['tagline']} {p['summary']} {p['description']}", p)
        add(f"{p['id']}:features", "product_features", f"{n} features, specifications and integrations",
            "Key features: " + "; ".join(p["keyFeatures"]) + ". "
            + "Specifications: " + "; ".join(f"{k}: {v}" for k, v in p["specs"].items()) + ". "
            + "Integrations: " + ", ".join(p["integrations"]) + ".", p)
        for t in p["tiers"]:
            m = annual_math(t)
            savings = (f" Billed annually the yearly cost is {money(m['year_if_annual'])} instead of "
                       f"{money(m['year_if_monthly'])}, saving {money(m['saved'])} ({pct(m['saved_pct'])}) per user."
                       if m else "")
            limits = (" Limits: " + "; ".join(f"{k}: {v}" for k, v in t["limits"].items()) + "."
                      if t.get("limits") else "")
            add(f"{p['id']}:tier:{slug(t['name'])}", "tier", f"{n} {t['name']} plan",
                f"Price: {tier_price_label(t)}.{savings} Includes: {', '.join(t['highlights'])}.{limits}", p)
        if p.get("addOns"):
            add(f"{p['id']}:addons", "addon", f"{n} add-ons",
                f"Optional add-ons for {n}, purchased on top of any plan: "
                + " ".join(f"{a['name']} add-on: {a['price']}. {a['desc']}" for a in p["addOns"]), p)
        for i, f in enumerate(p["faqs"], 1):
            add(f"{p['id']}:faq:{i}", "faq", f"{n} FAQ: {f['q']}", f["a"], p)

    ids = [ch["id"] for ch in chunks]
    assert len(ids) == len(set(ids)), "duplicate chunk ids"
    return chunks


# ---------------------------------------------------------------- main

def count_tokens(text: str) -> tuple[int, str]:
    try:
        import tiktoken
        return len(tiktoken.get_encoding("o200k_base").encode(text)), "tiktoken o200k_base"
    except Exception:
        return len(text) // 4, "estimate chars/4"


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", type=Path, default=ROOT / "data" / "catalog.json")
    ap.add_argument("--out", type=Path, default=ROOT / "knowledge")
    args = ap.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)

    docs = build_docs(catalog)
    for name, text in docs.items():
        write(args.out / name, text)
    context = build_context(docs)
    write(args.out / "context.md", context)
    chunks = build_chunks(catalog)
    write(args.out / "chunks.jsonl", "".join(json.dumps(ch, ensure_ascii=False) + "\n" for ch in chunks))

    print(f"{'file':<24}{'bytes':>9}{'tokens':>9}")
    for name in [n for n, _ in DOC_ORDER] + ["context.md"]:
        text = (args.out / name).read_text(encoding="utf-8")
        print(f"{name:<24}{len(text.encode()):>9}{count_tokens(text)[0]:>9}")
    tok, method = count_tokens(context)
    chunk_tok = [count_tokens(ch["text"])[0] for ch in chunks]
    types: dict[str, int] = {}
    for ch in chunks:
        types[ch["type"]] = types.get(ch["type"], 0) + 1
    print(f"\ncontext.md: {tok} tokens ({method})")
    print(f"chunks.jsonl: {len(chunks)} chunks, tokens min/avg/max "
          f"{min(chunk_tok)}/{sum(chunk_tok) // len(chunk_tok)}/{max(chunk_tok)}; {types}")


if __name__ == "__main__":
    main()
