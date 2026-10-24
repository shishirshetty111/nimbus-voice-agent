// Page renderers. Each builds DOM from the catalog into #app.
import { el, tone, initials, fromPrice, param, escapeHtml, icon, catIconName, CART_MODULE } from "./app.js";

const app = () => document.querySelector("#app");

// Add-to-cart (client-side store; loaded on demand).
function addToCartUI(productName, tier) {
  import(CART_MODULE).then((m) => m.addToCart(productName, tier || null, 1)).catch((e) => console.error(e));
}

// Map a category id to a tone index so a product's tile colour matches its family.
function catTone(catalog, categoryId) {
  const idx = (catalog.categories || []).findIndex((c) => c.id === categoryId);
  return tone(idx < 0 ? 0 : idx);
}

function productCard(p, toneClass) {
  const price = fromPrice(p);
  return el("a", { href: `product.html?id=${p.id}`, class: "card product-card reveal" }, [
    el("div", { class: `pc-icon ${toneClass}` }, initials(p.name)),
    el("div", { class: "pc-cat" }, p.category),
    el("h3", {}, p.name),
    el("div", { class: "pc-sum" }, p.summary || p.tagline),
    el("div", { class: "pc-foot" }, [
      el("span", { class: "pc-price", html: `${price.label} <small>${price.sub}</small>` }),
      el("span", { class: "pc-view", html: `View ${icon("arrow")}` }),
    ]),
  ]);
}

// ---------------- Home ----------------
export function renderHome(catalog) {
  const c = catalog.company;
  const cats = catalog.categories || [];
  const stats = Object.entries(c.stats || {});

  // Hero visual: a glass "console" panel showing the app suite.
  const tiles = cats
    .slice(0, 6)
    .map(
      (cat, i) =>
        `<div class="app-tile"><span class="ic ${tone(i)}">${icon(catIconName(cat.id))}</span><b>${escapeHtml(
          cat.name
        )}</b></div>`
    )
    .join("");
  const uptime = c.stats?.Uptime || "99.99%";
  const customers = c.stats?.Customers || "50,000+ businesses";

  const heroStats = stats
    .map(([k, v]) => `<div><div class="num">${escapeHtml(v)}</div><div class="lbl">${escapeHtml(k)}</div></div>`)
    .join("");

  app().append(
    el("section", {
      class: "hero",
      html: `<div class="container">
        <div class="hero-grid">
          <div>
            <span class="pill pill-brand">${icon("sparkle")} The all-in-one cloud suite</span>
            <h1>Run your <span class="grad-text">whole business</span> in the cloud</h1>
            <p class="lead">${escapeHtml(
              c.about ||
                "Nimbus brings sales, finance, HR, support, and analytics into one connected platform, so your teams work from a single source of truth."
            )}</p>
            <div class="hero-actions">
              <a href="products.html" class="btn btn-primary">Explore products ${icon("arrow")}</a>
              <a href="agent.html" class="btn btn-ghost" data-open-assistant>${icon("mic")} Talk to our voice agent</a>
              <a href="pricing.html" class="btn btn-ghost">See pricing</a>
            </div>
            <div class="hero-trust"><span class="pill"><span class="dot"></span>${escapeHtml(
              uptime
            )} uptime</span> Trusted by ${escapeHtml(customers)}</div>
            <div class="hero-stats">${heroStats}</div>
          </div>
          <div class="hero-visual">
            <div class="hero-panel">
              <div class="hero-panel-bar"><i></i><i></i><i></i><span>nimbus.app</span></div>
              <div class="app-grid">${tiles}</div>
            </div>
            <div class="hero-float float-a"><span class="ic tone-2">${icon(
              "clock"
            )}</span><div>${escapeHtml(uptime)}<small>uptime SLA</small></div></div>
            <div class="hero-float float-b"><span class="ic tone-0">${icon(
              "building"
            )}</span><div>${escapeHtml(customers)}<small>on Nimbus</small></div></div>
          </div>
        </div>
      </div>`,
    })
  );

  // Trust strip
  app().append(
    el("div", {
      class: "trust-strip",
      html: `<div class="container">
        <div class="trust-cap">Powering teams across ${escapeHtml(c.stats?.Countries || "120+")} countries</div>
        <div class="trust-logos">
          <span>Northwind</span><span>Acme Corp</span><span>Globex</span>
          <span>Initech</span><span>Umbrella</span><span>Soylent</span><span>Hooli</span>
        </div>
      </div>`,
    })
  );

  // Why Nimbus (bento)
  const features = [
    ["puzzle", "One connected suite", "Every app shares the same data model, so a new lead in CRM flows straight through to invoicing and support.", true],
    ["bolt", "Live in days, not months", "Pre-built workflows and sensible defaults get teams productive fast, with no lengthy implementation."],
    ["shield", "Secure by default", "SOC 2 controls, SSO, encryption at rest and in transit, and granular role-based access on every app."],
    ["globe", "Built for global teams", `Multi-currency, multi-language, and data residency across ${escapeHtml(c.stats?.Countries || "120+")} countries.`],
    ["refresh", "Scales with you", "Start with one app on a free tier and add more as you grow, all under one bill and one login.", true],
  ];
  const bento = el("div", { class: "bento" });
  features.forEach(([ic, title, body, wide], i) => {
    bento.append(
      el("div", {
        class: `feature reveal ${wide ? "wide" : ""}`,
        html: `<div class="ic ${tone(i)}">${icon(ic)}</div><h3>${title}</h3><p>${body}</p>`,
      })
    );
  });
  app().append(
    el("section", { class: "section" }, [
      el("div", { class: "container" }, [
        el("div", { class: "section-head center reveal" }, [
          el("div", { class: "eyebrow" }, "Why Nimbus"),
          el("h2", {}, "One platform behind every part of your business"),
          el("p", {}, "Stop stitching together a dozen disconnected tools. Nimbus apps are designed to work as one."),
        ]),
        bento,
      ]),
    ])
  );

  // Product families
  const catGrid = el("div", { class: "grid grid-4" });
  cats.forEach((cat, i) => {
    const count = catalog.products.filter((p) => p.categoryId === cat.id).length;
    catGrid.append(
      el("a", { href: `products.html?cat=${cat.id}`, class: "card cat-card reveal" }, [
        el("div", { class: `ic ${tone(i)}`, html: icon(catIconName(cat.id)) }),
        el("h3", {}, cat.name),
        el("div", { class: "muted" }, `${count} apps`),
        el("span", { class: "arrow", html: `Browse ${icon("arrow")}` }),
      ])
    );
  });
  app().append(
    el("section", { class: "section section-soft" }, [
      el("div", { class: "container" }, [
        el("div", { class: "section-head reveal" }, [
          el("div", { class: "eyebrow" }, "Product families"),
          el("h2", {}, "Everything your team needs, under one roof"),
        ]),
        catGrid,
      ]),
    ])
  );

  // Featured products
  const featGrid = el("div", { class: "grid grid-3" });
  catalog.products.slice(0, 6).forEach((p) => featGrid.append(productCard(p, catTone(catalog, p.categoryId))));
  app().append(
    el("section", { class: "section" }, [
      el("div", { class: "container" }, [
        el("div", { class: "section-head reveal" }, [
          el("div", { class: "eyebrow" }, "Popular apps"),
          el("h2", {}, "Start with a flagship, add more anytime"),
        ]),
        featGrid,
        el("div", {
          class: "center reveal",
          html: `<a href="products.html" class="btn btn-ghost" style="margin-top:1.8rem">View all ${catalog.products.length} products ${icon(
            "arrow"
          )}</a>`,
        }),
      ]),
    ])
  );

  // CTA band
  app().append(
    el("section", { class: "section" }, [
      el("div", { class: "container" }, [
        el("div", {
          class: "cta-band reveal",
          html: `<div class="eyebrow">Free for 14 days</div>
            <h2>Ready to bring your business onto ${escapeHtml(c.name)}?</h2>
            <p>No credit card required. Start free, invite your team, and add apps whenever you need them.</p>
            <div style="display:flex;gap:.8rem;justify-content:center;flex-wrap:wrap">
              <a href="products.html" class="btn btn-light">Explore products ${icon("arrow")}</a>
              <a href="pricing.html" class="btn btn-light">See plans and pricing</a>
            </div>`,
        }),
      ]),
    ])
  );
}

// ---------------- Products listing ----------------
export function renderProducts(catalog) {
  const activeCat = param("cat") || "all";

  app().append(
    el("section", { class: "hero", style: "padding:72px 0 56px" }, [
      el("div", { class: "container" }, [
        el("div", { class: "eyebrow" }, "Product catalog"),
        el("h1", {}, `All ${catalog.products.length} Nimbus apps`),
        el("p", { class: "lead" }, "Pick a family or browse everything. Every app has a free tier and a 14-day trial on paid plans."),
      ]),
    ])
  );

  app().append(
    el("section", { class: "section" }, [
      el("div", { class: "container" }, [
        el("div", { class: "chips", id: "chips" }),
        el("div", { class: "grid grid-3", id: "pgrid" }),
      ]),
    ])
  );

  const chips = document.querySelector("#chips");
  const grid = document.querySelector("#pgrid");

  const makeChip = (id, label, iconName) =>
    el("button", {
      class: `chip ${id === activeCat ? "active" : ""}`,
      html: `${iconName ? icon(iconName) : ""}${escapeHtml(label)}`,
      onclick: () => {
        location.href = id === "all" ? "products.html" : `products.html?cat=${id}`;
      },
    });

  chips.append(makeChip("all", `All (${catalog.products.length})`));
  (catalog.categories || []).forEach((cat) => {
    const n = catalog.products.filter((p) => p.categoryId === cat.id).length;
    chips.append(makeChip(cat.id, `${cat.name} (${n})`, catIconName(cat.id)));
  });

  const list =
    activeCat === "all"
      ? catalog.products
      : catalog.products.filter((p) => p.categoryId === activeCat);
  list.forEach((p) => grid.append(productCard(p, catTone(catalog, p.categoryId))));
}

// ---------------- Product detail ----------------
export function renderProductDetail(catalog) {
  const id = param("id");
  const p = catalog.products.find((x) => x.id === id);
  if (!p) {
    app().innerHTML = `<div class="container loading"><h2>Product not found</h2><p><a href="products.html">Back to all products</a></p></div>`;
    return;
  }
  document.title = `${p.name} · Nimbus`;
  const toneClass = catTone(catalog, p.categoryId);

  app().append(
    el("section", { class: "detail-hero" }, [
      el("div", { class: "container" }, [
        el("div", {
          class: "crumbs",
          html: `<a href="products.html">Products</a> / <a href="products.html?cat=${p.categoryId}">${escapeHtml(
            p.category
          )}</a> / ${escapeHtml(p.name)}`,
        }),
        el("div", { class: "detail-head" }, [
          el("div", { class: `pc-icon ${toneClass}` }, initials(p.name)),
          el("div", {}, [
            el("h1", { style: "margin-bottom:.3rem;font-size:clamp(2rem,4vw,2.8rem)" }, p.name),
            el("p", { class: "lead", style: "font-size:1.15rem;margin:0;color:var(--slate)" }, p.tagline),
          ]),
        ]),
      ]),
    ])
  );

  // Body grid: left = description/features/specs/faqs, right = sticky tiers summary
  const left = el("div", {});
  left.append(el("p", { class: "lead", style: "color:var(--slate);font-size:1.08rem" }, p.summary));
  (p.description || "").split(/\n+/).filter(Boolean).forEach((para) => left.append(el("p", {}, para)));

  left.append(el("h3", { style: "margin-top:1.8rem" }, "Key features"));
  const fl = el("ul", { class: "feature-list" });
  (p.keyFeatures || []).forEach((f) => fl.append(el("li", {}, f)));
  left.append(fl);

  if (p.integrations?.length) {
    left.append(el("h3", { style: "margin-top:1.8rem" }, "Integrations"));
    const tags = el("div", { class: "tag-row" });
    p.integrations.forEach((it) => tags.append(el("span", { class: "tag" }, it)));
    left.append(tags);
  }

  left.append(el("h3", { style: "margin-top:1.8rem" }, "Specifications"));
  const tbl = el("table", { class: "spec-table" });
  Object.entries(p.specs || {}).forEach(([k, v]) =>
    tbl.append(el("tr", {}, [el("td", {}, k), el("td", {}, String(v))]))
  );
  left.append(tbl);

  if (p.faqs?.length) {
    left.append(el("h3", { style: "margin-top:1.8rem" }, "Frequently asked"));
    p.faqs.forEach((f) =>
      left.append(
        el("details", { class: "faq" }, [el("summary", {}, f.q), el("div", { class: "faq-body" }, f.a)])
      )
    );
  }

  // Right: pricing quick view
  const side = el("div", { class: "card side-card" });
  const price = fromPrice(p);
  side.append(el("div", { class: "muted", html: "Starting at" }));
  side.append(
    el("div", { style: "margin:.2rem 0 .1rem", html: `<span class="price-big">${price.label}</span> <span class="muted">${price.sub}</span>` })
  );
  side.append(el("p", { class: "muted", style: "font-size:.88rem;margin-top:.4rem" }, "Billed annually. Free tier available."));
  side.append(
    el("button", { class: "btn btn-primary btn-block", style: "margin:.8rem 0 .5rem", onclick: () => addToCartUI(p.name) }, "Add to cart")
  );
  side.append(el("a", { href: "#pricing", class: "btn btn-ghost btn-block" }, "Compare plans"));
  if (p.addOns?.length) {
    side.append(el("div", { class: "t-limits", style: "margin-top:1.2rem", html: "<strong>Add-ons</strong>" }));
    p.addOns.forEach((a) =>
      side.append(el("div", { class: "muted", style: "font-size:.85rem;margin-top:.45rem" }, `${a.name}: ${a.price}`))
    );
  }

  app().append(el("section", {}, [el("div", { class: "container detail-grid" }, [left, side])]));

  // Full pricing tiers
  app().append(
    el("section", { class: "section section-soft", id: "pricing" }, [
      el("div", { class: "container" }, [
        el("div", { class: "section-head" }, [
          el("div", { class: "eyebrow" }, "Pricing"),
          el("h2", {}, `${p.name} plans`),
        ]),
        tiersGrid(p.tiers || [], p.name),
        catalog.policies?.refund
          ? el("p", { class: "muted center", style: "margin-top:1.6rem" }, "Covered by our 30-day money-back guarantee.")
          : null,
      ]),
    ])
  );

  app().append(relatedSection(catalog, p));
}

function tiersGrid(tiers, productName = null) {
  const grid = el("div", { class: "tiers" });
  tiers.forEach((t) => {
    const featured = /pro/i.test(t.name);
    const isCustom = t.custom || t.priceMonthly == null;
    const priceHtml = isCustom
      ? `Custom`
      : t.priceAnnualMonthly === 0 || t.priceMonthly === 0
      ? `$0`
      : `$${t.priceAnnualMonthly ?? t.priceMonthly}<small> ${t.unit || "/mo"}</small>`;
    const lims = Object.entries(t.limits || {})
      .map(([k, v]) => `${k}: ${v}`)
      .join(" · ");
    grid.append(
      el("div", { class: `tier ${featured ? "featured" : ""}` }, [
        featured ? el("div", { class: "badge-pop" }, "Most popular") : null,
        el("div", { class: "t-name" }, t.name),
        el("div", { class: "t-price", html: priceHtml }),
        el("div", { class: "t-unit" }, isCustom ? "Contact sales" : t.priceMonthly === 0 ? "Free forever" : "billed annually"),
        el("ul", {}, (t.highlights || []).map((h) => el("li", {}, h))),
        lims ? el("div", { class: "t-limits" }, lims) : null,
        tierAction(t, featured, isCustom, productName),
      ])
    );
  });
  return grid;
}

// The tier call-to-action: paid tiers add to the cart; free/custom keep their label.
function tierAction(t, featured, isCustom, productName) {
  const cls = `btn ${featured ? "btn-primary" : "btn-ghost"} btn-block`;
  const isFree = t.priceMonthly === 0;
  if (productName && !isCustom && !isFree) {
    return el("button", { class: cls, onclick: () => addToCartUI(productName, t.name) }, "Add to cart");
  }
  return el("a", { href: isCustom ? "support.html" : "#", class: cls }, isCustom ? "Contact sales" : "Start free");
}

function relatedSection(catalog, p) {
  const related = catalog.products.filter((x) => x.categoryId === p.categoryId && x.id !== p.id);
  if (!related.length) return el("span");
  const grid = el("div", { class: "grid grid-3" });
  related.forEach((r) => grid.append(productCard(r, catTone(catalog, r.categoryId))));
  return el("section", { class: "section" }, [
    el("div", { class: "container" }, [
      el("div", { class: "section-head" }, [el("h2", {}, `More in ${p.category}`)]),
      grid,
    ]),
  ]);
}

// ---------------- Pricing overview ----------------
export function renderPricing(catalog) {
  const pol = catalog.policies || {};
  app().append(
    el("section", { class: "hero", style: "padding:80px 0 56px" }, [
      el("div", { class: "container" }, [
        el("div", { class: "eyebrow" }, "Pricing"),
        el("h1", {}, "Simple, transparent pricing"),
        el("p", { class: "lead" }, "Every Nimbus app follows the same four-tier model: a free plan to get started, Starter and Professional for growing teams, and Enterprise for custom needs. Pick per app, billed monthly or annually (save around 20%)."),
      ]),
    ])
  );

  const sample = catalog.products[0];
  if (sample) {
    app().append(
      el("section", { class: "section" }, [
        el("div", { class: "container" }, [
          el("div", { class: "section-head center reveal" }, [
            el("h2", {}, "The four Nimbus plans"),
            el("p", { class: "muted" }, "Shown for a typical app. Exact limits and prices vary by product."),
          ]),
          tiersGrid(sample.tiers || []),
        ]),
      ])
    );
  }

  // Per-product starting prices, grouped by category
  const wrap = el("div", { class: "container" });
  (catalog.categories || []).forEach((cat) => {
    const items = catalog.products.filter((p) => p.categoryId === cat.id);
    if (!items.length) return;
    const grid = el("div", { class: "grid grid-3" });
    items.forEach((p) => {
      const price = fromPrice(p);
      grid.append(
        el("a", { href: `product.html?id=${p.id}#pricing`, class: "card product-card reveal" }, [
          el("h3", {}, p.name),
          el("div", { class: "pc-sum" }, p.tagline),
          el("div", { class: "pc-foot" }, [
            el("span", { class: "pc-price", html: `from <strong>${price.label}</strong> <small>${price.sub}</small>` }),
            el("span", { class: "pc-view", html: `Plans ${icon("arrow")}` }),
          ]),
        ])
      );
    });
    wrap.append(el("h3", { style: "margin:2.2rem 0 1rem" }, cat.name), grid);
  });
  app().append(el("section", { class: "section section-soft" }, [wrap]));

  // Billing and guarantee notes
  app().append(
    el("section", { class: "section" }, [
      el("div", { class: "container grid grid-2" }, [
        policyBlock("card", "Billing", pol.billing),
        policyBlock("refresh", "30-day money-back guarantee", pol.refund),
      ]),
    ])
  );
}

function policyBlock(iconName, title, body, toneClass = "tone-0") {
  return el("div", { class: "policy-block reveal" }, [
    el("h3", {}, [el("span", { class: `ic ${toneClass}`, html: icon(iconName) }), title]),
    el("p", { class: "muted" }, body || "Details available on request."),
  ]);
}

// ---------------- Support / policies ----------------
export function renderSupport(catalog) {
  const pol = catalog.policies || {};
  const c = catalog.company;
  app().append(
    el("section", { class: "hero", style: "padding:80px 0 56px" }, [
      el("div", { class: "container" }, [
        el("div", { class: "eyebrow" }, "Support and policies"),
        el("h1", {}, "We have got your back"),
        el("p", { class: "lead" }, "Find our refund, billing, security, and service-level policies below, plus the fastest ways to reach a human."),
      ]),
    ])
  );

  const blocks = [
    ["refresh", "Refund policy", pol.refund],
    ["flask", "Free trial", pol.freeTrial],
    ["card", "Billing and payments", pol.billing],
    ["clock", "Cancellation", pol.cancellation],
    ["bolt", "Service-level agreement (SLA)", pol.sla],
    ["shield", "Security and compliance", pol.security],
    ["globe", "Data residency", pol.dataResidency],
    ["chat", "Support channels", pol.support],
  ].filter(([, , body]) => body);

  const grid = el("div", { class: "grid grid-2" });
  blocks.forEach(([ic, title, body], i) => grid.append(policyBlock(ic, title, body, tone(i))));
  app().append(el("section", { class: "section" }, [el("div", { class: "container" }, [grid])]));

  // Aggregated FAQs from products
  const faqs = [];
  catalog.products.forEach((p) => (p.faqs || []).forEach((f) => faqs.push({ ...f, product: p.name })));
  const refundFaqs = faqs.filter((f) => /refund|trial|cancel|cost|price/i.test(f.q)).slice(0, 10);
  if (refundFaqs.length) {
    const list = el("div", {});
    refundFaqs.forEach((f) =>
      list.append(el("details", { class: "faq" }, [el("summary", {}, f.q), el("div", { class: "faq-body" }, f.a)]))
    );
    app().append(
      el("section", { class: "section section-soft" }, [
        el("div", { class: "container" }, [
          el("div", { class: "section-head" }, [el("h2", {}, "Frequently asked questions")]),
          list,
        ]),
      ])
    );
  }

  // Contact band
  app().append(
    el("section", { class: "section" }, [
      el("div", { class: "container" }, [
        el("div", {
          class: "cta-band reveal",
          html: `<h2>Still need help?</h2>
            <p>Sales: ${escapeHtml(c.contact?.sales || "")} &middot; Support: ${escapeHtml(
            c.contact?.support || ""
          )} &middot; ${escapeHtml(c.contact?.phone || "")}</p>
            <a href="products.html" class="btn btn-light">Browse products ${icon("arrow")}</a>`,
        }),
      ]),
    ])
  );
}

// ---------------- About ----------------
export function renderAbout(catalog) {
  const c = catalog.company;
  const stats = Object.entries(c.stats || {})
    .map(([k, v]) => `<div><div class="num">${escapeHtml(v)}</div><div class="lbl">${escapeHtml(k)}</div></div>`)
    .join("");
  app().append(
    el("section", { class: "hero", style: "padding:80px 0 56px" }, [
      el("div", { class: "container" }, [
        el("div", { class: "eyebrow" }, `About ${escapeHtml(c.name)}`),
        el("h1", {}, c.tagline || c.name),
        el("p", { class: "lead" }, c.about || ""),
        el("div", { class: "hero-stats", html: stats }),
      ]),
    ])
  );
  app().append(
    el("section", { class: "section" }, [
      el("div", { class: "container grid grid-2" }, [
        policyBlock("target", "Our mission", c.mission || "", "tone-0"),
        policyBlock("building", "Headquarters", `${c.hq || ""}. Founded ${c.founded || ""}.`, "tone-1"),
      ]),
    ])
  );
  // category overview
  const grid = el("div", { class: "grid grid-4" });
  (catalog.categories || []).forEach((cat, i) => {
    const n = catalog.products.filter((p) => p.categoryId === cat.id).length;
    grid.append(
      el("a", { href: `products.html?cat=${cat.id}`, class: "card cat-card reveal" }, [
        el("div", { class: `ic ${tone(i)}`, html: icon(catIconName(cat.id)) }),
        el("h3", {}, cat.name),
        el("div", { class: "muted" }, `${n} apps`),
      ])
    );
  });
  app().append(
    el("section", { class: "section section-soft" }, [
      el("div", { class: "container" }, [
        el("div", { class: "section-head reveal" }, [
          el("h2", {}, `${catalog.products.length} products across ${catalog.categories.length} families`),
        ]),
        grid,
      ]),
    ])
  );
}
