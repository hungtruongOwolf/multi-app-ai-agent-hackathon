(() => {
  "use strict";

  const state = { products: [], categories: [], category: "All", cart: loadCart(), query: "", current: null };
  const $ = (id) => document.getElementById(id);
  const money = (n) => `$${Number(n).toFixed(2)}`;

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "style") node.style.cssText = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return node;
  }
  const art = (p) => `background: linear-gradient(135deg, ${p.colors[0]}, ${p.colors[1]})`;

  function loadCart() {
    try { return JSON.parse(localStorage.getItem("shoplab.cart") || "{}"); } catch { return {}; }
  }
  function saveCart() {
    try { localStorage.setItem("shoplab.cart", JSON.stringify(state.cart)); } catch { /* private mode */ }
  }

  async function api(path, options = {}) {
    const res = await fetch(`/shop/api${path}`, {
      headers: { "Content-Type": "application/json" }, ...options,
    });
    let body = null;
    try { body = await res.json(); } catch { body = null; }
    if (!res.ok || (body && body.ok === false)) {
      const err = new Error((body && body.message) || "Something went wrong. Please try again.");
      err.reference = body && body.reference;
      throw err;
    }
    return body;
  }

  // ---------------------------------------------------------------- toasts
  function toast(message, { error = false, reference = null } = {}) {
    const node = el("div", { class: `toast${error ? " error" : ""}`, role: "status" },
      el("span", { class: "icon" }, error ? "⚠️" : "✓"),
      el("div", {}, message, reference ? el("span", { class: "ref" }, `Reference ${reference}`) : ""));
    $("toasts").append(node);
    setTimeout(() => node.remove(), error ? 6500 : 3000);
  }

  // ---------------------------------------------------------------- catalog
  function renderChips() {
    const chips = $("chips");
    chips.replaceChildren();
    for (const cat of ["All", ...state.categories]) {
      chips.append(el("button", {
        class: "chip", role: "tab", "aria-selected": String(cat === state.category),
        onclick: () => { state.category = cat; state.query = ""; $("search-input").value = ""; render(); },
      }, cat));
    }
  }

  function card(p) {
    return el("article", { class: "card" },
      el("button", { class: "card-art", style: art(p), "aria-label": `View ${p.name}`, onclick: () => openProduct(p.id) }, p.emoji),
      el("div", { class: "card-body" },
        el("span", { class: "card-cat" }, p.category),
        el("button", { class: "card-name", onclick: () => openProduct(p.id) }, p.name),
        el("div", { class: "card-foot" },
          el("span", { class: "price" }, money(p.price)),
          el("button", { class: "add", onclick: () => addToCart(p.id) }, "Add"))));
  }

  function render(list = null) {
    renderChips();
    const items = list || state.products.filter((p) => state.category === "All" || p.category === state.category);
    $("grid-title").textContent = state.query ? `Results for “${state.query}”` : (state.category === "All" ? "All products" : state.category);
    const grid = $("grid");
    grid.replaceChildren(...items.map(card));
    if (!items.length) grid.append(el("p", { class: "muted" }, "No products match your search."));
  }

  async function search(q) {
    state.query = q.trim();
    const notice = $("search-notice");
    notice.hidden = true;
    if (!state.query) { render(); return; }
    try {
      const body = await api(`/search?q=${encodeURIComponent(state.query)}`);
      state.category = "All";
      render(body.results);
    } catch (e) {
      notice.className = "notice warn";
      notice.textContent = e.message;
      notice.hidden = false;
      render(state.products.filter((p) => p.name.toLowerCase().includes(state.query.toLowerCase())));
      toast(e.message, { error: true, reference: e.reference });
    }
  }

  // ---------------------------------------------------------------- product dialog
  async function openProduct(id) {
    const p = state.products.find((x) => x.id === id);
    if (!p) return;
    state.current = p;
    $("pd-art").style.cssText = art(p);
    $("pd-art").textContent = p.emoji;
    $("pd-cat").textContent = p.category;
    $("pd-name").textContent = p.name;
    $("pd-price").textContent = money(p.price);
    $("pd-blurb").textContent = p.blurb;
    const st = $("pd-state");
    st.className = "pd-state skeleton";
    st.textContent = "Checking availability…";
    $("pd-add").disabled = true;
    show("product");
    try {
      const body = await api(`/products/${id}`);
      $("pd-price").textContent = money(body.product.price);
      st.className = "pd-state";
      st.innerHTML = "";
      st.append(el("span", { style: "color:#047857;font-weight:600" }, "● In stock"), " — order today, ships tomorrow");
      $("pd-add").disabled = false;
    } catch (e) {
      st.className = "pd-state";
      st.replaceChildren(el("span", { style: "color:#b91c1c" }, e.message));
      $("pd-add").disabled = false;
      toast(e.message, { error: true, reference: e.reference });
    }
  }

  // ---------------------------------------------------------------- cart
  function addToCart(id) {
    state.cart[id] = (state.cart[id] || 0) + 1;
    saveCart();
    renderCart();
    const p = state.products.find((x) => x.id === id);
    toast(`${p ? p.name : "Item"} added to cart`);
  }

  function cartLines() {
    return Object.entries(state.cart)
      .map(([id, qty]) => ({ p: state.products.find((x) => x.id === Number(id)), qty }))
      .filter((l) => l.p && l.qty > 0);
  }

  function renderCart() {
    const lines = cartLines();
    const count = lines.reduce((n, l) => n + l.qty, 0);
    const subtotal = lines.reduce((n, l) => n + l.qty * l.p.price, 0);
    $("cart-count").textContent = String(count);
    $("cart-count").hidden = count === 0;
    $("subtotal").textContent = money(subtotal);
    $("shipping").textContent = subtotal >= 75 || subtotal === 0 ? "Free" : money(6.9);
    $("checkout-btn").disabled = count === 0;
    const body = $("cart-lines");
    if (!lines.length) {
      body.replaceChildren(el("div", { class: "empty" }, el("div", { style: "font-size:40px" }, "🛍️"), "Your cart is empty."));
      return;
    }
    body.replaceChildren(...lines.map(({ p, qty }) => el("div", { class: "line" },
      el("div", { class: "line-art", style: art(p) }, p.emoji),
      el("div", {},
        el("div", { class: "line-name" }, p.name),
        el("div", { class: "qty" },
          el("button", { "aria-label": "Decrease", onclick: () => { state.cart[p.id] = qty - 1; if (state.cart[p.id] <= 0) delete state.cart[p.id]; saveCart(); renderCart(); } }, "−"),
          el("span", {}, qty),
          el("button", { "aria-label": "Increase", onclick: () => { state.cart[p.id] = qty + 1; saveCart(); renderCart(); } }, "+"))),
      el("b", {}, money(p.price * qty)))));
  }

  // ---------------------------------------------------------------- checkout
  function openCheckout() {
    const lines = cartLines();
    const subtotal = lines.reduce((n, l) => n + l.qty * l.p.price, 0);
    const shipping = subtotal >= 75 ? 0 : 6.9;
    const summary = $("co-summary");
    summary.replaceChildren(
      ...lines.map(({ p, qty }) => el("div", { class: "row" }, el("span", {}, `${qty} × ${p.name}`), el("span", {}, money(qty * p.price)))),
      el("div", { class: "row muted" }, el("span", {}, "Shipping"), el("span", {}, shipping ? money(shipping) : "Free")),
      el("div", { class: "row total" }, el("span", {}, "Total"), el("span", {}, money(subtotal + shipping))));
    $("pay-btn").textContent = `Pay ${money(subtotal + shipping)}`;
    $("co-error").hidden = true;
    $("checkout-form-view").hidden = false;
    $("checkout-done-view").hidden = true;
    hide("cart");
    show("checkout");
  }

  async function pay() {
    const btn = $("pay-btn");
    const label = btn.textContent;
    btn.classList.add("loading");
    btn.textContent = "Processing…";
    $("co-error").hidden = true;
    try {
      const items = cartLines().map(({ p, qty }) => ({ id: p.id, qty }));
      const body = await api("/pay", { method: "POST", body: JSON.stringify({ items, email: "jordan.demo@shoplab.test" }) });
      state.cart = {};
      saveCart();
      renderCart();
      $("order-id").textContent = body.order_id;
      $("checkout-form-view").hidden = true;
      $("checkout-done-view").hidden = false;
    } catch (e) {
      const box = $("co-error");
      box.replaceChildren(e.message, e.reference ? el("small", {}, `Reference ${e.reference}`) : "");
      box.hidden = false;
      toast(e.message, { error: true, reference: e.reference });
    } finally {
      btn.classList.remove("loading");
      btn.textContent = label;
    }
  }

  // ---------------------------------------------------------------- avatar
  async function uploadAvatar() {
    try {
      await api("/avatar", { method: "POST", body: "{}" });
      toast("Profile photo updated");
    } catch (e) {
      toast(e.message, { error: true, reference: e.reference });
    }
  }

  // ---------------------------------------------------------------- overlays
  const overlays = {
    product: ["product-scrim", "product-dialog"],
    cart: ["cart-scrim", "drawer"],
    checkout: ["checkout-scrim", "checkout-dialog"],
  };
  function show(name) { overlays[name].forEach((id) => { $(id).hidden = false; }); }
  function hide(name) { overlays[name].forEach((id) => { $(id).hidden = true; }); }

  function wire() {
    $("cart-btn").addEventListener("click", () => { renderCart(); show("cart"); });
    $("checkout-btn").addEventListener("click", openCheckout);
    $("pay-btn").addEventListener("click", pay);
    $("pd-add").addEventListener("click", () => { if (state.current) { addToCart(state.current.id); hide("product"); } });
    $("account-btn").addEventListener("click", () => $("avatar-file").click());
    $("avatar-file").addEventListener("change", () => { uploadAvatar(); $("avatar-file").value = ""; });
    $("product-scrim").addEventListener("click", () => hide("product"));
    $("cart-scrim").addEventListener("click", () => hide("cart"));
    $("checkout-scrim").addEventListener("click", () => hide("checkout"));
    document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => hide(b.dataset.close)));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") Object.keys(overlays).forEach(hide); });
    let timer = null;
    $("search-form").addEventListener("submit", (e) => { e.preventDefault(); search($("search-input").value); });
    $("search-input").addEventListener("input", (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => search(e.target.value), 350);
    });
  }

  async function boot() {
    wire();
    try {
      const body = await api("/catalog");
      state.products = body.products;
      state.categories = body.categories;
    } catch (e) {
      toast("We couldn't load the catalog. Please refresh.", { error: true, reference: e.reference });
    }
    render();
    renderCart();
  }

  boot();
})();
