(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { faults: [], active: [], filter: "all", busy: false };

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of children) if (c !== null && c !== undefined) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return node;
  }

  const pct = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`);
  const ms = (v) => (v === null || v === undefined ? "—" : v < 1 ? `${Math.round(v * 1000)} ms` : `${v.toFixed(2)} s`);
  const rps = (v) => (v === null || v === undefined ? "—" : v.toFixed(1));
  const hhmmss = (iso) => new Date(iso).toLocaleTimeString([], { hour12: false });

  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, 2600);
  }

  async function getJSON(url) {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(`${url} → ${res.status}`);
    return res.json();
  }

  async function post(url, body) {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (!res.ok) throw new Error((data && (data.detail || data.message)) || `request failed (${res.status})`);
    return data;
  }

  // ---------------------------------------------------------------- health
  async function refreshHealth() {
    try {
      const data = await getJSON("/ops/api/health");
      $("health-hint").textContent = data.available ? "live · last 20 s · updates every 2 s" : "collecting metrics…";
      $("tiles").replaceChildren(...data.services.map((s) => {
        const flags = Object.entries(s.flags || {}).map(([k, v]) => el("span", { class: `tag${v ? " hot" : ""}` }, `${k}=${v}`));
        return el("div", { class: `tile ${s.status}` },
          el("div", { class: "tile-head" }, el("span", { class: "svc" }, s.service), el("span", { class: `status ${s.status}` }, s.status)),
          el("div", { class: "metrics" },
            el("div", { class: "metric" }, el("div", { class: "v" }, pct(s.error_rate)), el("div", { class: "k" }, "errors")),
            el("div", { class: "metric" }, el("div", { class: "v" }, ms(s.latency_p95)), el("div", { class: "k" }, "p95")),
            el("div", { class: "metric" }, el("div", { class: "v" }, rps(s.rps)), el("div", { class: "k" }, "req/s"))),
          el("div", { class: "cfg" },
            el("span", { class: `tag${s.pool_size !== null && s.pool_size < 10 ? " hot" : ""}` }, `pool=${s.pool_size}`),
            el("span", { class: `tag${s.version !== "1.4.1" ? " hot" : ""}` }, `v${s.version}`),
            ...flags));
      }));
    } catch {
      $("health-hint").textContent = "supervisor unreachable";
    }
  }

  // ---------------------------------------------------------------- faults
  function renderFilters() {
    const kinds = [["all", "All"], ["change", "Bad changes"], ["infra", "Infrastructure"]];
    $("filters").replaceChildren(...kinds.map(([key, label]) => el("button", {
      class: "filter", "aria-pressed": String(state.filter === key),
      onclick: () => { state.filter = key; renderFaults(); renderFilters(); },
    }, label)));
  }

  function renderFaults() {
    const activeIds = new Set(state.active.map((a) => a.fault));
    const list = state.faults.filter((f) => state.filter === "all" || f.kind === state.filter);
    $("faults").replaceChildren(...list.map((f) => {
      const on = activeIds.has(f.id);
      return el("article", { class: `fault${on ? " on" : ""}` },
        el("div", { class: "fault-head" },
          el("div", {},
            el("h3", {}, f.title),
            el("div", { class: "meta" },
              el("span", { class: `sev ${f.severity}` }, f.severity),
              el("span", { class: "tag" }, f.service),
              el("span", { class: "tag" }, f.kind === "change" ? "recorded as a change" : "infrastructure"))),
          el("button", {
            class: `btn${on ? "" : " danger"}`, disabled: on ? "" : null,
            onclick: (e) => inject(f, e.currentTarget),
          }, on ? "Active" : "Inject")),
        el("dl", { class: "dl" },
          el("div", {}, el("dt", {}, "What breaks"), el("dd", {}, f.breaks)),
          el("div", {}, el("dt", {}, "Customers"), el("dd", {}, f.customers)),
          el("div", {}, el("dt", {}, "Agent should"), el("dd", {}, f.agent))));
    }));
  }

  function renderActive() {
    const box = $("active");
    if (!state.active.length) {
      box.replaceChildren(el("span", { class: "none" }, "No faults injected — ShopLab is healthy."));
      return;
    }
    box.replaceChildren(...state.active.map((a) => el("span", { class: "chip" },
      el("span", { class: "dot" }),
      (a.info && a.info.title) || a.fault,
      el("span", { class: "when" }, `since ${hhmmss(a.at)}`))));
  }

  async function inject(f, button) {
    if (state.busy) return;
    state.busy = true;
    button.disabled = true;
    try {
      await post("/ops/faults", { fault: f.id });
      toast(`Injected: ${f.title}`);
      await refreshState();
    } catch (e) {
      toast(e.message);
      button.disabled = false;
    } finally {
      state.busy = false;
    }
  }

  async function clearAll() {
    const btn = $("clear-btn");
    btn.disabled = true;
    try {
      await post("/ops/faults/clear");
      toast("All faults cleared; defaults restored");
      await refreshState();
      await refreshChanges();
    } catch (e) {
      toast(e.message);
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- state, changes, customer errors
  async function refreshState() {
    try {
      const data = await getJSON("/ops/api/state");
      state.faults = data.faults;
      state.active = data.active;
      $("trial-pill").textContent = `run ${data.trial_id || "default"}`;
      const tr = data.traffic || {};
      $("traffic-pill").textContent = tr.enabled === false ? "traffic paused" : `traffic ${tr.rps ?? "—"} req/s`;
      renderActive();
      renderFaults();
      renderCustomerErrors(data.customer_errors || []);
    } catch { /* supervisor restarting */ }
  }

  function renderCustomerErrors(items) {
    const list = $("customer-errors");
    if (!items.length) {
      list.replaceChildren(el("li", { class: "empty" }, "No customer-facing errors yet. Try checking out on the storefront."));
      return;
    }
    list.replaceChildren(...items.map((e) => el("li", {},
      el("span", { class: "ts" }, hhmmss(e.ts)),
      el("div", {},
        el("div", { class: "what" }, e.message),
        el("div", { class: "who" }, `${e.journey} · HTTP ${e.status} · upstream `, el("span", { class: "code" }, e.upstream || "—"),
          " · ref ", el("span", { class: "code" }, e.ref))))));
  }

  async function refreshChanges() {
    try {
      const items = await getJSON("/changes");
      const list = $("changes");
      if (!items.length) {
        list.replaceChildren(el("li", { class: "empty" }, "No changes yet."));
        return;
      }
      list.replaceChildren(...items.slice().reverse().slice(0, 60).map((c) => el("li", {},
        el("span", { class: "ts" }, hhmmss(c.ts)),
        el("div", {},
          el("div", { class: "what" }, el("span", { class: "code" }, c.service), " ", c.summary),
          el("div", { class: "who" }, `${c.kind} by `, el("span", { class: `actor${c.actor === "incident-judge" ? " agent" : ""}` }, c.actor))))));
    } catch { /* ignore */ }
  }

  function boot() {
    renderFilters();
    $("clear-btn").addEventListener("click", clearAll);
    const tick = () => { refreshHealth(); refreshState(); refreshChanges(); };
    tick();
    setInterval(tick, 2000);
  }

  boot();
})();
