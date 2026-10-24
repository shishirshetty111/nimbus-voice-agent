// Shopping cart for the Nimbus site.
//
// When the Nimbus backend is reachable the cart lives on the server, keyed by a random cart id
// kept in localStorage and sent as X-Cart-Id. That is the same cart the voice agent's tools
// (add_to_cart, checkout, ...) change, so a click here and "add it to my cart" said to the agent
// stay in sync. Without a backend (e.g. the static Vercel site) it falls back to localStorage.
//
// Other scripts can call refreshCart() or dispatch a "nimbus:cart-changed" event after changing
// the cart server-side; the drawer then re-reads it.

import { el, escapeHtml, icon, loadCatalog } from "./app.js";
import { apiBase } from "./voice/api.js";
import { getCartId } from "./voice/cart-id.js";

export { getCartId };

const LOCAL_KEY = "nimbus_cart";

let drawer, badge, listEl, totalEl, dueEl, statusEl;
let backend = null; // null = not probed yet; true/false after probing

async function hasBackend() {
  if (backend !== null) return backend;
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 1500);
    const res = await fetch(apiBase() + "/api/health", { signal: ctl.signal });
    clearTimeout(timer);
    backend = res.ok;
  } catch {
    backend = false;
  }
  return backend;
}

async function server(path, body) {
  const res = await fetch(apiBase() + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Cart-Id": getCartId(), ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data.detail && (data.detail.error || data.detail)) || `HTTP ${res.status}`);
  return data;
}

// ---- local fallback storage ----
function loadLocal() {
  try {
    const items = JSON.parse(localStorage.getItem(LOCAL_KEY) || "[]");
    return Array.isArray(items) ? items : [];
  } catch {
    return [];
  }
}
function saveLocal(items) {
  try {
    localStorage.setItem(LOCAL_KEY, JSON.stringify(items));
  } catch {}
}

// Normalized line: {line_id, product_name, tier, seats, billing, unit_price}
const lineMonthly = (it) => (Number(it.unit_price) || 0) * (Number(it.seats) || 1);
const lineDueToday = (it) => lineMonthly(it) * (it.billing === "annual" ? 12 : 1);
const sum = (items, f) => items.reduce((a, it) => a + f(it), 0);
const money = (n) => "$" + (Math.round(n * 100) / 100).toLocaleString();

async function readCart() {
  if (await hasBackend()) {
    try {
      return (await server("/api/cart")).lines;
    } catch {
      backend = false;
    }
  }
  return loadLocal();
}

// ---- public API ----
export async function addToCart(productName, tierName = null, seats = 1, billing = "annual") {
  try {
    if (await hasBackend()) {
      await server("/api/cart/add", { product: productName, tier: tierName || undefined, seats, billing });
    } else {
      await addLocal(productName, tierName, seats, billing);
    }
    await refreshCart();
    openCart();
    toast(`Added ${productName}${tierName ? ` (${tierName})` : ""}`);
  } catch (err) {
    toast(err.message);
  }
}

async function addLocal(productName, tierName, seats, billing) {
  let product, tier;
  try {
    const catalog = await loadCatalog();
    product = (catalog.products || []).find((p) => p.name === productName);
    const tiers = (product && product.tiers) || [];
    tier = tierName ? tiers.find((t) => t.name === tierName) : tiers.filter((t) => (t.priceMonthly || 0) > 0).sort((a, b) => a.priceMonthly - b.priceMonthly)[0] || tiers[0];
  } catch {}
  const unit = (tier && (billing === "annual" ? tier.priceAnnualMonthly : tier.priceMonthly)) || 0;
  const tierLabel = (tier && tier.name) || tierName || "Standard";
  const items = loadLocal();
  const same = items.find((i) => i.product_name === productName && i.tier === tierLabel && i.billing === billing);
  if (same) same.seats += seats;
  else items.push({ line_id: Math.random().toString(36).slice(2, 10), product_name: productName, tier: tierLabel, seats, billing, unit_price: unit });
  saveLocal(items);
}

export async function refreshCart() {
  paint(await readCart());
}

export function mountCart() {
  if (document.querySelector(".cart-btn")) return;

  const btn = el("button", {
    class: "cart-btn",
    "aria-label": "Open cart",
    html: `${icon("card")}<span class="cart-count" hidden>0</span>`,
    onclick: openCart,
  });
  const cta = document.querySelector(".nav-cta");
  if (cta) cta.insertBefore(btn, cta.firstChild);
  badge = btn.querySelector(".cart-count");

  drawer = el("div", {
    class: "cart-drawer",
    html: `<div class="cart-scrim"></div>
      <aside class="cart-panel" role="dialog" aria-label="Your cart">
        <header class="cart-head"><h3>Your cart</h3><button class="cart-x" aria-label="Close">${icon("arrow")}</button></header>
        <div class="cart-status"></div>
        <div class="cart-items"></div>
        <footer class="cart-foot">
          <div class="cart-total"><span>Per month</span><strong>$0</strong></div>
          <div class="cart-due"><span>Due today</span><span class="cart-due-v">$0</span></div>
          <button class="btn btn-primary btn-block cart-checkout">Checkout (dummy)</button>
          <p class="cart-note">No real payment is taken. This is a teaching demo.</p>
        </footer>
      </aside>`,
  });
  document.body.append(drawer);
  listEl = drawer.querySelector(".cart-items");
  totalEl = drawer.querySelector(".cart-total strong");
  dueEl = drawer.querySelector(".cart-due-v");
  statusEl = drawer.querySelector(".cart-status");
  drawer.querySelector(".cart-scrim").addEventListener("click", closeCart);
  drawer.querySelector(".cart-x").addEventListener("click", closeCart);
  drawer.querySelector(".cart-checkout").addEventListener("click", checkout);

  // Pick up changes made elsewhere (the agent, another tab).
  window.addEventListener("nimbus:cart-changed", refreshCart);
  window.addEventListener("focus", refreshCart);
  window.nimbusCart = { refresh: refreshCart, open: openCart, id: getCartId };

  refreshCart();
}

// ---- drawer ----
export function openCart() {
  if (!drawer) return;
  drawer.classList.add("open");
  document.body.style.overflow = "hidden";
  refreshCart();
}
function closeCart() {
  drawer.classList.remove("open");
  document.body.style.overflow = "";
}

async function checkout() {
  const items = await readCart();
  if (!items.length) {
    statusEl.innerHTML = `<div class="cart-empty">Your cart is empty.</div>`;
    return;
  }
  let orderId, total = sum(items, lineDueToday);
  try {
    if (await hasBackend()) {
      const res = await server("/api/cart/checkout", {});
      orderId = res.order.order_id;
      total = res.order.totals.due_today;
    } else {
      orderId = "NB-" + Math.random().toString(36).slice(2, 8).toUpperCase();
      saveLocal([]);
    }
  } catch (err) {
    statusEl.innerHTML = `<div class="cart-empty">${escapeHtml(err.message)}</div>`;
    return;
  }
  await refreshCart();
  statusEl.innerHTML = `<div class="cart-done">${icon("shield")}<div><strong>Order ${escapeHtml(orderId)} confirmed</strong><span>${items.length} app(s), ${money(total)} due today. Dummy checkout, no card charged.</span></div></div>`;
}

async function removeLine(lineId) {
  try {
    if (await hasBackend()) await server("/api/cart/remove", { line_id: lineId });
    else saveLocal(loadLocal().filter((i) => i.line_id !== lineId));
  } catch (err) {
    toast(err.message);
  }
  refreshCart();
}

function paint(items) {
  setBadge(sum(items, (it) => Number(it.seats) || 1));
  if (!listEl) return;
  totalEl.textContent = money(sum(items, lineMonthly));
  dueEl.textContent = money(sum(items, lineDueToday));
  if (!items.length) {
    listEl.innerHTML = `<div class="cart-empty">Your cart is empty. Add an app from any product or pricing page.</div>`;
    return;
  }
  if (statusEl && !statusEl.querySelector(".cart-done")) statusEl.innerHTML = "";
  listEl.innerHTML = "";
  items.forEach((it) =>
    listEl.append(
      el("div", {
        class: "cart-item",
        html: `<div><strong>${escapeHtml(it.product_name)}</strong><span>${escapeHtml(it.tier)} &middot; ${it.seats} seat(s) &middot; billed ${
          it.billing === "monthly" ? "monthly" : "annually"
        }</span></div>
          <div class="cart-item-right"><span>${money(lineMonthly(it))}/mo</span>
          <button class="cart-remove" aria-label="Remove ${escapeHtml(it.product_name)}">${icon("arrow")}</button></div>`,
      })
    )
  );
  listEl.querySelectorAll(".cart-remove").forEach((b, i) => b.addEventListener("click", () => removeLine(items[i].line_id)));
}

function setBadge(n) {
  if (!badge) return;
  badge.textContent = String(n);
  badge.hidden = !n;
}

let toastEl;
function toast(msg) {
  toastEl?.remove();
  toastEl = el("div", { class: "toast", html: escapeHtml(msg) });
  document.body.append(toastEl);
  setTimeout(() => toastEl?.classList.add("show"), 10);
  setTimeout(() => {
    toastEl?.classList.remove("show");
    setTimeout(() => toastEl?.remove(), 300);
  }, 2400);
}
