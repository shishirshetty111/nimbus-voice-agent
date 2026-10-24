// The cart id shared by the cart drawer and the voice agent. A random id kept in localStorage
// and sent to the backend as X-Cart-Id, so the same server-side cart is used everywhere.

const ID_KEY = "nimbus_cart_id";
let memoryId = null; // fallback when localStorage is unavailable

const newId = () =>
  "c_" + (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : Math.random().toString(36).slice(2) + Date.now().toString(36));

export function getCartId() {
  try {
    let id = localStorage.getItem(ID_KEY);
    if (!id) {
      id = newId();
      localStorage.setItem(ID_KEY, id);
    }
    return id;
  } catch {
    return (memoryId ||= newId());
  }
}
