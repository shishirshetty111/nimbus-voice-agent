// Core: catalog loading, shared layout (nav/footer), icons, and small helpers.

// The cart module's URL carries a version so browsers never reuse a stale cached copy from before
// it became server-backed. Bump it when cart.js's exports change. Every importer must use this
// same URL, or the page would load two separate cart modules.
export const CART_MODULE = new URL("./cart.js?v=2", import.meta.url).href;

// Resolve assets/data relative to THIS module, so the site works no matter
// which subpath it is mounted at (root, a nested folder, Live Preview, etc.).
const asset = (rel) => new URL(rel, import.meta.url).href;

const NAV_LINKS = [
  { href: "products.html", label: "Products" },
  { href: "pricing.html", label: "Pricing" },
  { href: "support.html", label: "Support" },
  { href: "about.html", label: "Company" },
  { href: "agent.html", label: "Assistant" },
  { href: "rag.html", label: "RAG Lab" },
  { href: "playground.html", label: "Playground" },
];

// ---- Inline SVG icon set (stroke = currentColor) ----
const ICONS = {
  "sales-crm": '<path d="M22 7l-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
  marketing: '<path d="M3 11l16-6v14L3 13z"/><path d="M11.5 18.5a3 3 0 0 1-5.7-1.3"/><path d="M19 9a3 3 0 0 1 0 6"/>',
  finance: '<path d="M12 1v22"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
  "hr-people": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  support: '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8z"/>',
  projects: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  analytics: '<path d="M3 3v18h18"/><rect x="7" y="11" width="3" height="6" rx="1"/><rect x="12" y="7" width="3" height="10" rx="1"/><rect x="17" y="13" width="3" height="4" rx="1"/>',
  "it-security": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>',
  bolt: '<path d="M13 2L4.5 13.5H11l-1 8.5L19.5 10H13z"/>',
  puzzle: '<path d="M19 11h-1.5a2 2 0 1 0-4 0H8a2 2 0 0 0-2 2v3.5a2 2 0 1 1 0 4V21h11a2 2 0 0 0 2-2v-3.5a2 2 0 1 0 0-4z"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20z"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/>',
  card: '<rect x="2" y="5" width="20" height="14" rx="2.5"/><path d="M2 10h20"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.4"/>',
  building: '<rect x="4" y="3" width="16" height="18" rx="1.5"/><path d="M9 8h.01M15 8h.01M9 12h.01M15 12h.01M9 16h6"/>',
  chat: '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8z"/>',
  mic: '<rect x="9" y="2" width="6" height="11" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><path d="M12 17v4"/><path d="M8 21h8"/>',
  flask: '<path d="M9 3h6"/><path d="M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4A2 2 0 0 0 19 18l-5-9V3"/><path d="M7.5 14h9"/>',
  arrow: '<path d="M5 12h14"/><path d="M13 6l6 6-6 6"/>',
  sparkle: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/>',
};

export function icon(name, cls = "") {
  const body = ICONS[name] || ICONS.sparkle;
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

export const catIconName = (id) => (ICONS[id] ? id : "sparkle");

let _catalog = null;

// Load and cache the catalog JSON. Validates the shape at the boundary.
export async function loadCatalog() {
  if (_catalog) return _catalog;
  const url = asset("../data/catalog.json");
  let res;
  try {
    res = await fetch(url, { cache: "no-cache" });
  } catch (networkErr) {
    throw new Error("Could not reach the catalog. Serve the folder over HTTP (not file://) and try again.");
  }
  if (!res.ok) throw new Error(`Failed to load catalog (HTTP ${res.status})`);
  let data;
  try {
    data = await res.json();
  } catch {
    throw new Error("The catalog file is not valid JSON.");
  }
  if (!data || !Array.isArray(data.products) || !data.company) {
    throw new Error("Catalog JSON is missing required fields.");
  }
  _catalog = data;
  return data;
}

export const tone = (i) => `tone-${((i % 8) + 8) % 8}`;

export const initials = (name) =>
  name.replace(/^Nimbus\s+/i, "").trim().slice(0, 2).toUpperCase();

export function fromPrice(product) {
  const paid = (product.tiers || []).filter((t) => typeof t.priceMonthly === "number" && t.priceMonthly > 0);
  if (!paid.length) return { label: "Free", sub: "" };
  const min = Math.min(...paid.map((t) => t.priceAnnualMonthly ?? t.priceMonthly));
  return { label: `$${min}`, sub: "/user/mo" };
}

export const param = (k) => new URLSearchParams(location.search).get(k);

// Tiny DOM builder. el("div", {class:"x"}, [child, "text"])
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

export const escapeHtml = (s = "") =>
  s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Brand mark (image tile) used in nav + footer.
const brandMark = (size = 34) =>
  `<img class="logo" src="${asset("favicon.svg")}" alt="Nimbus logo" width="${size}" height="${size}" />`;

// ---- Shared layout ----
export function mountHeader(active = "") {
  const links = NAV_LINKS.map(
    (l) => `<a href="${l.href}" class="${l.label.toLowerCase() === active ? "active" : ""}">${l.label}</a>`
  ).join("");
  const header = el("div", {
    class: "nav",
    html: `<div class="container nav-inner">
      <a href="index.html" class="brand">${brandMark(34)} Nimbus</a>
      <nav class="nav-links">${links}</nav>
      <div class="nav-cta">
        <a href="pricing.html" class="btn btn-ghost btn-sm">Pricing</a>
        <a href="products.html" class="btn btn-primary btn-sm">Get started</a>
      </div>
    </div>`,
  });
  document.body.prepend(header);

  // Subtle border/shadow once the page is scrolled.
  const onScroll = () => header.classList.toggle("scrolled", window.scrollY > 8);
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });
}

export function mountFooter(catalog) {
  const c = catalog.company;
  const cats = (catalog.categories || [])
    .slice(0, 6)
    .map((cat) => `<li><a href="products.html?cat=${cat.id}">${escapeHtml(cat.name)}</a></li>`)
    .join("");
  const footer = el("footer", {
    class: "footer",
    html: `<div class="container">
      <div class="footer-cols">
        <div>
          <div class="brand">${brandMark(32)} ${escapeHtml(c.name)}</div>
          <p class="footer-tag">${escapeHtml(c.tagline || "")}</p>
          <p class="fictitious">A fictitious company built for the Voice Agents bootcamp (Session 7).</p>
        </div>
        <div><h4>Products</h4><ul>${cats}</ul></div>
        <div><h4>Company</h4><ul>
          <li><a href="about.html">About</a></li>
          <li><a href="pricing.html">Pricing</a></li>
          <li><a href="support.html">Support</a></li>
        </ul></div>
        <div><h4>Contact</h4><ul>
          <li>${escapeHtml(c.contact?.sales || "")}</li>
          <li>${escapeHtml(c.contact?.support || "")}</li>
          <li>${escapeHtml(c.contact?.phone || "")}</li>
        </ul></div>
      </div>
      <div class="footer-bottom">
        <span>&copy; ${escapeHtml(String(c.founded || ""))} to present. ${escapeHtml(c.legalName || c.name)} Not a real company.</span>
        <span>${escapeHtml(c.hq || "")}</span>
      </div>
    </div>`,
  });
  document.body.append(footer);
}

// Animate elements with .reveal into view as they enter the viewport.
function observeReveals() {
  const els = document.querySelectorAll(".reveal");
  if (!("IntersectionObserver" in window) || !els.length) {
    els.forEach((e) => e.classList.add("in"));
    return;
  }
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          io.unobserve(entry.target);
        }
      });
    },
    { rootMargin: "0px 0px -8% 0px", threshold: 0.08 }
  );
  els.forEach((e) => io.observe(e));
}

// Standard page bootstrap: header + content render + footer, with error handling.
export async function bootstrap(active, renderFn) {
  try {
    const catalog = await loadCatalog();
    mountHeader(active);
    await renderFn(catalog);
    mountFooter(catalog);
    observeReveals();
    // Client-side cart (localStorage); loaded lazily so browsing never blocks on it.
    import(CART_MODULE).then((m) => m.mountCart()).catch(() => {});
  } catch (err) {
    console.error(err);
    const main = document.querySelector("#app") || document.body;
    main.innerHTML = `<div class="container loading">
      <h2>Something went wrong</h2>
      <p class="muted">${escapeHtml(err.message)}</p>
    </div>`;
  }
}
