// The shops' console: one shop at a time (or all three), the numbers first.
(() => {
  "use strict";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  const ALL = { id: "all", name: "All shops", color: "" };
  let shops = [];
  let shop = null;        // the selected shop's id, or "all"
  let section = "overview";
  let orders = [];
  let events = [];
  let selected = null;    // the open order's id
  const filters = { attention: "all", state: "all", door: "all", rail: "all" };
  // Orders seen by this page, to notice the ones that arrive while it's open.
  let known = null;
  let baseTitle = document.title;

  // ---- Helpers

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value == null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "shop") node.style.setProperty("--shop", value);
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    node.append(...children.flat(Infinity).filter((c) => c != null && c !== false));
    return node;
  }
  const money = (cents) => {
    const whole = Math.floor(Math.abs(cents) / 100);
    const rest = Math.abs(cents) % 100;
    return `${cents < 0 ? "-" : ""}$${whole}${rest ? "." + String(rest).padStart(2, "0") : ""}`;
  };
  const fmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const when = (iso) => (iso ? fmt.format(new Date(iso)) : "–");
  const shortId = (id) => (id ? id.slice(0, 8) : "");
  const pill = (text, kind) => el("span", { class: `pill ${kind}`, text });
  const logo = (shopId, cls = "") => el("img", { class: `logo ${cls}`, src: `/api/${shopId}/logo.svg`, alt: "" });
  const words = (s) => (s ?? "").replaceAll("_", " ");
  const host = (url) => { try { return new URL(url).host; } catch { return url ?? ""; } };
  const percent = (part, whole) => (whole ? `${Math.round((part / whole) * 100)}%` : "–");

  function status(text, kind = "", ms = 6000) {
    const node = $("#status");
    node.textContent = text;
    node.className = `status ${kind}`;
    if (text) setTimeout(() => { if (node.textContent === text) node.textContent = ""; }, ms);
  }

  async function api(path, options = {}, shopId = shop) {
    const response = await fetch(`/api/${shopId}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
      body: options.body == null ? undefined : JSON.stringify(options.body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail ?? `HTTP ${response.status}`);
    return data;
  }

  function copyButton(text) {
    return el("button", { class: "copy", type: "button", title: "Copy", "aria-label": "Copy", text: "⧉", onclick: async (e) => {
      e.stopPropagation();
      try { await navigator.clipboard.writeText(text); status("Copied.", "good"); } catch { status("Couldn't copy.", "bad"); }
    } });
  }

  // ---- Overview

  function kpi(label, value, note) {
    return el("div", { class: "kpi" }, el("span", { class: "label", text: label }), el("b", { text: value }), note && el("small", { text: note }));
  }

  function chart(days) {
    const top = Math.max(...days.map((d) => d.amount), 1);
    const bars = days.map((d) => {
      const h = Math.round((d.amount / top) * 100);
      const r = Math.round((d.refunded / top) * 100);
      const date = new Date(d.day + "T00:00:00");
      return el("div", { class: "stick", title: `${d.day}: ${d.orders} order${d.orders === 1 ? "" : "s"}, ${money(d.amount)}${d.refunded ? `, ${money(d.refunded)} refunded` : ""}` },
        el("div", { class: "col" },
          el("div", { class: "fill", style: `height:${h}%` }),
          r ? el("div", { class: "fill bad", style: `height:${r}%` }) : null),
        el("span", { class: "day", text: date.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) }),
        el("span", { class: "n", text: d.orders || "" }));
    });
    return bars;
  }

  function factList(entries) {
    if (!entries.length) return [el("dt", { class: "muted", text: "Nothing yet" }), el("dd")];
    return entries.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", {}, v)]);
  }

  async function loadStats() {
    try {
      const s = await api("/stats");
      const agent = s.doors.agent ?? 0;
      const verified = s.signatures.verified ?? 0;
      $("#stats-note").textContent = `${s.orders} orders · ${s.checkouts_opened} checkouts opened`;
      $("#kpis").replaceChildren(
        kpi("Net card revenue", money(s.net), `${money(s.gross)} taken, ${money(s.refunded)} refunded`),
        kpi("Orders", s.orders, `${agent} by agents, ${s.doors.web ?? 0} on the web`),
        kpi("Agent share", percent(agent, s.orders), `${verified} of ${agent} signatures verified`),
        kpi("Coins", `${s.coins_spent} spent`, `${s.coins_earned} earned back by customers`),
        kpi("Declined payments", s.declines, Object.entries(s.decline_codes).map(([c, n]) => `${words(c).toLowerCase()} ×${n}`).join(", ") || "none"),
        kpi("Checkout conversion", percent(s.checkouts_paid, s.checkouts_opened), `${s.checkouts_paid} paid of ${s.checkouts_opened} opened`));
      $("#chart").replaceChildren(...chart(s.days));
      $("#doors").replaceChildren(...factList([
        ["Agents (UCP)", `${agent} · ${percent(agent, s.orders)}`],
        ["Web", `${s.doors.web ?? 0} · ${percent(s.doors.web ?? 0, s.orders)}`],
        ...Object.entries(s.signatures).map(([k, n]) => [`Signature ${k}`, n]),
      ]));
      $("#redemption").replaceChildren(...factList(Object.entries(s.redemption).map(([k, n]) => [({ unredeemed: "Ready to use", redeemed: "Used at the venue", cancelled_by_merchant: "Cancelled by the merchant", redemption_failed: "Not honoured", refunded: "Refunded, void" })[k] ?? words(k), n])));
      $("#payments").replaceChildren(...factList([
        ...Object.entries(s.rails).map(([k, n]) => [`Rail ${k}`, `${n} orders`]),
        ["Declined", s.declines],
        ["Refunded", money(s.refunded)],
      ]));
    } catch (error) { status(error.message, "bad"); }
  }

  // ---- Orders

  function doorCell(order) {
    // The full version, and a one-pill version for when the panel is open.
    if (order.channel === "agent") {
      const sig = { verified: ["signature verified", "good"], failed: ["signature failed", "bad"], missing: ["unsigned", "warn"] }[order.signature] ?? [order.signature ?? "agent", "plain"];
      const mark = { verified: "✓", failed: "✕", missing: "?" }[order.signature] ?? "";
      return [el("span", { class: "full" }, pill("Agent", "plain"), " ", pill(sig[0], sig[1]), el("small", { text: host(order.agent) })),
        el("span", { class: "compact" }, pill(`Agent ${mark}`.trim(), sig[1]))];
    }
    const hints = order.visit?.hints ?? [];
    const suspect = order.visit?.agent || hints.length;
    return [el("span", { class: "full" }, pill("Web", "plain"), order.visit?.agent && [" ", pill(`declared ${order.visit.agent}`, "warn")], hints.length ? el("small", { text: hints.join(", ") }) : null),
      el("span", { class: "compact" }, pill(suspect ? "Web ?" : "Web", suspect ? "warn" : "plain"))];
  }

  function stateCell(order) {
    const parts = [];
    if (order.payment.status === "refunded") parts.push(pill("Refunded", "bad"));
    else if (order.payment.status === "captured") parts.push(pill("Paid", "good"));
    else parts.push(pill(order.payment.status ?? "?", "plain"));
    if (order.payment.risk?.review) parts.push(" ", pill("Fraud check: review", "warn"));
    for (const item of order.items) {
      const redemption = { unredeemed: ["ready", "plain"], redeemed: ["used", "good"], cancelled_by_merchant: ["cancelled", "bad"], redemption_failed: ["not honoured", "bad"] }[item.redemption];
      if (item.voucher === "refunded") parts.push(" ", pill("void", "bad"));
      else if (redemption) parts.push(" ", pill(redemption[0], redemption[1]));
    }
    return parts;
  }

  function orderState(o) {
    if (o.payment.status === "refunded") return "refunded";
    if (o.items.some((i) => i.redemption === "cancelled_by_merchant" || i.redemption === "redemption_failed")) return "cancelled";
    if (o.items.some((i) => i.redemption === "redeemed")) return "used";
    return "ready";
  }

  function keep(o) {
    const state = orderState(o);
    if (filters.attention === "todo" && o.attention === "handled") return false;
    if (["new", "handled"].includes(filters.attention) && o.attention !== filters.attention) return false;
    if (filters.state === "paid" && o.payment.status !== "captured") return false;
    if (["ready", "used", "cancelled", "refunded"].includes(filters.state) && state !== filters.state) return false;
    if (filters.door === "agent" && o.channel !== "agent") return false;
    if (filters.door === "web" && o.channel === "agent") return false;
    if (filters.door === "suspect" && !(o.channel !== "agent" && (o.visit?.agent || (o.visit?.hints ?? []).length))) return false;
    if (filters.rail !== "all" && o.payment.rail !== filters.rail) return false;
    const query = $("#order-search").value.trim().toLowerCase();
    return !query || JSON.stringify(o).toLowerCase().includes(query);
  }

  function drawRailChips() {
    const rails = [...new Set(orders.map((o) => o.payment.rail).filter(Boolean))].sort();
    const group = $("#rail-chips");
    group.replaceChildren(
      el("button", { type: "button", class: `chip ${filters.rail === "all" ? "on" : ""}`, "data-value": "all", text: "Any rail" }),
      ...rails.map((r) => el("button", { type: "button", class: `chip ${filters.rail === r ? "on" : ""}`, "data-value": r, text: `Rail ${r}` })));
    if (!rails.includes(filters.rail)) filters.rail = "all";
  }

  function drawOrders() {
    const all = shop === "all";
    for (const node of $$('#orders [data-col="shop"]')) node.hidden = !all;
    const rows = orders.filter(keep);
    const sum = rows.reduce((n, o) => n + (o.payment.status === "refunded" ? 0 : o.payment.amount), 0);
    $("#order-count").textContent = `${rows.length} of ${orders.length} · ${money(sum)} net`;
    const body = $("#orders tbody");
    body.replaceChildren(...rows.map((o) => el("tr", { class: `${o.id === selected ? "on" : ""} ${o.attention}`, "data-id": o.id, onclick: () => openOrder(o.id, o.shop) },
      el("td", { class: "flag" }, flag(o.attention)),
      el("td", {}, when(o.placed_at), el("small", { class: "mono", text: shortId(o.id) })),
      el("td", { "data-col": "shop", hidden: !all }, logo(o.shop, "small"), el("span", { class: "fold", text: " " + o.shop_name })),
      el("td", {}, o.buyer.full_name ?? "", el("small", { text: o.buyer.email ?? "" })),
      el("td", { class: "bought" }, o.items.map((i) => el("div", { text: `${i.quantity > 1 ? i.quantity + " × " : ""}${i.title}`, title: i.title }))),
      el("td", { class: "num" }, money(o.payment.amount), o.payment.coins ? el("small", { text: `+ ${o.payment.coins} coins` }) : null, o.payment.coins_earned ? el("small", { text: `earned ${o.payment.coins_earned}` }) : null, el("small", { class: "mono", text: o.payment.rail })),
      el("td", { class: "door" }, doorCell(o)),
      el("td", {}, stateCell(o)))));
    if (!rows.length) body.replaceChildren(el("tr", {}, el("td", { colspan: 8, class: "empty", text: orders.length ? "Nothing matches." : "No orders yet in this shop." })));
  }

  // The button in the panel's corner: mark handled by hand, or reopen.
  function markButton(order) {
    const s = order.summary;
    const handled = s.attention === "handled";
    const acted = s.payment.status === "refunded" || s.items.some((i) => i.redemption && i.redemption !== "unredeemed");
    if (handled && acted) return el("span", { class: "pill good handled-pill", text: "✓ handled" });
    return el("button", { class: "secondary small", type: "button", text: handled ? "Reopen" : "Mark handled", onclick: async () => {
      try {
        const mark = await api(`/orders/${s.id}/review`, { method: "POST", body: { state: handled ? "open" : "handled" } }, s.shop);
        const row = orders.find((o) => o.id === s.id);
        if (row) row.attention = mark.attention;
        drawOrders(); updateBadgeFromList();
        status(handled ? "Reopened." : "Marked handled.", "good");
        await openOrder(s.id, s.shop);
      } catch (error) { status(error.message, "bad"); }
    } });
  }

  function updateBadgeFromList() {
    if (shop === "all") { updateBadge(orders); return; }
    const count = orders.filter((o) => o.attention === "new").length;
    const own = $(`#shops [data-shop="${shop}"] .badge`);
    if (own) { own.textContent = count; own.hidden = !count; }
    // The total in the menu moves with it.
    const total = [...$$("#shops button:not([data-shop=all]) .badge")].reduce((n, b) => n + (b.hidden ? 0 : Number(b.textContent)), 0);
    $("#new-badge").textContent = total; $("#new-badge").hidden = !total;
    const all = $('#shops [data-shop="all"] .badge'); all.textContent = total; all.hidden = !total;
    document.title = (total ? `(${total}) ` : "") + baseTitle;
  }

  // The inbox mark: a dot for new, a ring for seen, a tick for handled.
  function flag(state) {
    const title = { new: "New: nobody has opened it", seen: "Seen, nothing done yet", handled: "Handled" }[state] ?? state;
    return el("span", { class: `mark ${state}`, title, "aria-label": title, text: state === "handled" ? "✓" : "" });
  }

  // ---- The inbox: a count in the menu and a word when an order arrives.

  // `rows` is every shop's orders: the menu gets the total, each shop its own.
  function updateBadge(rows) {
    const fresh = rows.filter((o) => o.attention === "new").length;
    const badge = $("#new-badge");
    badge.textContent = fresh;
    badge.hidden = !fresh;
    document.title = (fresh ? `(${fresh}) ` : "") + baseTitle;
    for (const button of $$("#shops button")) {
      const id = button.dataset.shop;
      const count = id === "all" ? fresh : rows.filter((o) => o.shop === id && o.attention === "new").length;
      const own = $(".badge", button);
      own.textContent = count;
      own.hidden = !count;
      own.title = `${count} order${count === 1 ? "" : "s"} to review`;
    }
  }

  async function watch() {
    try {
      const rows = await api("/orders", {}, "all");
      updateBadge(rows);
      if (known) {
        const arrived = rows.filter((o) => !known.has(o.id));
        if (arrived.length) {
          const o = arrived[0];
          status(arrived.length === 1
            ? `New order in ${o.shop_name}: ${o.items.map((i) => i.title).join(", ")} · ${money(o.payment.amount)}`
            : `${arrived.length} new orders arrived.`, "good", 12000);
          if (section === "orders") await loadOrders();
        }
      }
      known = new Set(rows.map((o) => o.id));
    } catch { /* The shops will answer next time. */ }
  }

  function loading(countId, bodySelector, columns) {
    $(countId).textContent = "Loading…";
    $(bodySelector).replaceChildren(el("tr", {}, el("td", { colspan: columns, class: "empty", text: "Loading…" })));
  }

  async function loadOrders() {
    loading("#order-count", "#orders tbody", 8);
    try {
      orders = await api("/orders");
      drawRailChips();
      drawOrders();
      if (shop === "all") { updateBadge(orders); known = new Set(orders.map((o) => o.id)); } else updateBadgeFromList();
    } catch (error) { status(error.message, "bad"); }
  }

  function exportCsv() {
    const rows = orders.filter(keep);
    const head = ["order_id", "shop", "placed_at", "buyer", "email", "items", "card_amount", "coins_spent", "coins_earned", "payment_status", "rail", "door", "signature", "voucher_state"];
    const cell = (v) => `"${String(v ?? "").replaceAll('"', '""')}"`;
    const lines = rows.map((o) => [
      o.id, o.shop_name, o.placed_at, o.buyer.full_name, o.buyer.email,
      o.items.map((i) => `${i.quantity} × ${i.title}`).join("; "),
      (o.payment.amount / 100).toFixed(2), o.payment.coins, o.payment.coins_earned,
      o.payment.status, o.payment.rail, o.channel, o.signature ?? "", orderState(o),
    ].map(cell).join(","));
    const blob = new Blob([[head.join(","), ...lines].join("\n")], { type: "text/csv" });
    const link = el("a", { href: URL.createObjectURL(blob), download: `orders-${shop}-${new Date().toISOString().slice(0, 10)}.csv` });
    link.click();
    URL.revokeObjectURL(link.href);
    status(`Exported ${rows.length} orders.`, "good");
  }

  // ---- One order

  function facts(order) {
    const s = order.summary;
    const rows = [
      ["Order", el("span", { class: "with-copy" }, el("code", { text: order.order.id }), copyButton(order.order.id))],
      ["Checkout", el("code", { text: order.order.checkout_id ?? "–" })],
      ["Placed", when(s.placed_at)],
      ["Buyer", `${s.buyer.full_name ?? ""} <${s.buyer.email ?? ""}>`],
      ["Door", s.channel === "agent" ? "Agent (UCP)" : "Web"],
    ];
    if (s.agent) rows.push(["Agent", el("code", { text: s.agent })]);
    if (order.order.signature) rows.push(["Signature", `${order.order.signature.status}${order.order.signature.keyid ? ` (key ${order.order.signature.keyid})` : ""}`]);
    if (s.visit) rows.push(["Web hints", [s.visit.agent && `declared as ${s.visit.agent}`, ...(s.visit.hints ?? [])].filter(Boolean).join(", ") || "none"]);
    return el("dl", { class: "facts" }, rows.map(([k, v]) => [el("dt", { text: k }), el("dd", {}, v)]));
  }

  // The vouchers: each line's codes, state and terms.
  function vouchers(order) {
    return el("div", { class: "vouchers" }, (order.order.line_items ?? []).map((line) => {
      const v = line.voucher ?? {};
      const r = line.redemption ?? {};
      const state = v.status === "refunded" ? pill("void", "bad")
        : { unredeemed: pill("ready to use", "plain"), redeemed: pill("used", "good"), cancelled_by_merchant: pill("cancelled", "bad"), redemption_failed: pill("not honoured", "bad") }[r.status] ?? pill(words(r.status), "plain");
      return el("div", { class: "voucher" },
        el("div", { class: "head" }, el("b", { text: line.item.title }), state),
        el("div", { class: "codes" }, (v.codes ?? []).map((c) => el("span", { class: "with-copy" }, el("code", { text: c }), copyButton(c)))),
        el("dl", { class: "facts" },
          line.service?.window?.not_before && [el("dt", { text: "Service" }), el("dd", { text: `${when(line.service.window.not_before)} → ${when(line.service.window.not_after)}` })],
          line.cancellation && [el("dt", { text: "Cancellation" }), el("dd", { text: words(line.cancellation.refundability) + (line.cancellation.refundable_until ? ` until ${when(line.cancellation.refundable_until)}` : "") })],
          v.expires_at && [el("dt", { text: "Expires" }), el("dd", { text: when(v.expires_at) })],
          line.redemption?.method && [el("dt", { text: "Redeem" }), el("dd", { text: words(line.redemption.method) })],
          r.at && [el("dt", { text: "Redeemed" }), el("dd", { text: when(r.at) })]));
    }));
  }

  // The strip of pills under the title: payment, voucher, door.
  function badges(order) {
    const s = order.summary;
    const out = [...stateCell(s)];
    const full = doorCell(s)[0];
    out.push(" ", ...[...full.children].filter((n) => n.tagName !== "SMALL"));
    return el("p", { class: "badges" }, out);
  }

  // A collapsible section of the panel.
  function part(title, open, ...children) {
    return el("details", { class: "part", open }, el("summary", {}, el("h4", { text: title })), el("div", { class: "part-body" }, ...children));
  }

  // What the rail (Stripe, or the mock) knows about the payment.
  function paymentPanel(order) {
    const s = order.summary.payment;
    const p = order.payment ?? {};
    const confirmed = order.events.find((e) => e.event === "PAYMENT_CONFIRMED");
    const box = el("div", { class: "payment" });
    const split = el("p", { class: "split-line" },
      el("b", { text: money(s.amount) }), " on the card",
      s.coins ? ` + ${s.coins} coins from the wallet` : "",
      s.coins_earned ? el("small", { text: ` · ${s.coins_earned} coins earned back` }) : null);
    box.append(split);
    if (p.error) {
      box.append(el("p", { class: "muted", text: `The rail could not be asked: ${p.error}` }));
      return box;
    }
    if (!p.id) {
      box.append(el("p", { class: "muted", text: s.amount ? "No payment on a card was recorded." : "Paid with coins alone: nothing on a card." }));
      return box;
    }
    const mode = { test: ["Stripe · test mode", "warn"], live: ["Stripe · live", "good"], simulated: ["Simulated · no money moved", "plain"] }[p.mode] ?? [p.rail, "plain"];
    const state = { authorised: ["authorised, not captured", "warn"], captured: ["captured", "good"], refunded: ["refunded", "bad"], partly_refunded: ["partly refunded", "warn"], canceled: ["authorisation cancelled", "bad"], failed: ["failed", "bad"] }[p.status];
    box.append(el("p", { class: "line" }, pill(mode[0], mode[1]), state ? [" ", pill(state[0], state[1])] : null));
    box.append(el("p", { class: "line with-copy" }, el("code", { text: p.id }), copyButton(p.id),
      p.url && el("a", { class: "ext", href: p.url, target: "_blank", rel: "noopener", text: "Open in Stripe ↗" })));
    if (p.card) {
      const brand = (p.card.brand ?? "card").replace(/^\w/, (c) => c.toUpperCase());
      box.append(el("p", { class: "line", text: `${brand} •••• ${p.card.last4}${p.card.exp_month ? ` · exp ${String(p.card.exp_month).padStart(2, "0")}/${String(p.card.exp_year).slice(-2)}` : ""}` }));
    }
    // Stripe's fraud check (Radar): what it said, and whether to look.
    const risk = p.risk ?? s.risk;
    if (risk) {
      const review = s.risk?.review;
      box.append(el("p", { class: "line" },
        pill(review ? "Fraud check: review" : `Fraud check: ${words(risk.level).toLowerCase()}`, review ? "warn" : risk.level === "normal" ? "good" : "plain"),
        el("small", { text: [risk.score != null ? `score ${risk.score}` : null, risk.outcome && words(risk.outcome).toLowerCase(), risk.reason && words(risk.reason).toLowerCase()].filter(Boolean).join(" · ") }),
        risk.seller_message ? el("small", { class: "muted", text: ` ${risk.seller_message}` }) : null));
    }
    if (p.mode !== "simulated") {
      const steps = [];
      if (p.created) steps.push([when(p.created), `authorised ${money(p.amount ?? 0)} ${p.currency ?? ""}`]);
      if (p.captured) steps.push([confirmed ? when(confirmed.at) : "", `captured ${money(p.captured)}`]);
      for (const r of p.refunds ?? []) steps.push([when(r.created), `refunded ${money(r.amount)} · ${r.status}${r.reason ? ` · ${words(r.reason)}` : ""}`, r.id]);
      if (p.status === "canceled") steps.push(["", "authorisation released; nothing was taken"]);
      box.append(el("ol", { class: "timeline" }, steps.map(([t, text, id]) => el("li", {}, t && el("time", { text: t }), text, id && el("small", { class: "mono muted", text: ` ${id}` })))));
    }
    return box;
  }

  function actions(order) {
    const s = order.summary;
    const refunded = s.payment.status === "refunded";
    const redeemed = s.items.some((i) => i.redemption === "redeemed");
    const cancelled = s.items.some((i) => i.redemption === "cancelled_by_merchant");
    const box = el("div", { class: "actions" });
    const confirmBox = el("div");
    const call = (path, options) => api(path, options, s.shop);
    const ask = (label, question, run, withReason = false) => {
      confirmBox.replaceChildren(el("div", { class: "confirm" },
        el("div", { text: question }),
        withReason && el("input", { name: "reason", placeholder: "Reason, for the record", "aria-label": "Reason" }),
        el("div", { class: "actions" },
          el("button", { class: "primary", type: "button", text: "Yes, go ahead", onclick: async () => { try { await run($("input", confirmBox)?.value ?? ""); status(`${label}: done.`, "good"); await refresh(true); } catch (error) { status(error.message, "bad"); } } }),
          el("button", { class: "secondary", type: "button", text: "No", onclick: () => confirmBox.replaceChildren() }))));
    };
    const back = `${money(s.payment.amount)} goes back to the card${s.payment.coins ? ` and ${s.payment.coins} coins to the wallet` : ""}${s.payment.coins_earned ? `; the ${s.payment.coins_earned} coins earned are taken back` : ""}.`;
    box.append(
      el("button", { class: "secondary", type: "button", text: "Mark redeemed", disabled: refunded || redeemed || cancelled, onclick: () => ask("Redeem", "Record that the customer used the voucher at the venue?", () => call(`/orders/${s.id}/redeem`, { method: "POST", body: { honoured: true } })) }),
      el("button", { class: "secondary", type: "button", text: "Not honoured at the venue", disabled: refunded || redeemed || cancelled, onclick: () => ask("Record a failed visit", "Record that the venue turned the customer away? A refund usually follows.", () => call(`/orders/${s.id}/redeem`, { method: "POST", body: { honoured: false } })) }),
      el("button", { class: "danger", type: "button", text: "Cancel the service", disabled: refunded || redeemed || cancelled, onclick: () => ask("Cancel", "Cancel the service on the merchant's side? The voucher can no longer be used.", () => call(`/orders/${s.id}/cancel`, { method: "POST" })) }),
      el("button", { class: "danger", type: "button", text: "Refund", disabled: refunded, onclick: () => ask("Refund", `Refund in full? ${back} The codes are voided.`, (reason) => call(`/orders/${s.id}/refund`, { method: "POST", body: { reason } }), true) }));
    return [box, confirmBox];
  }

  function timeline(events) {
    if (!events.length) return el("p", { class: "muted", text: "No events for this order." });
    return el("ol", { class: "timeline" }, events.map((e) => el("li", { class: e.event === "PAYMENT_DECLINED" ? "bad" : "" },
      el("time", { text: when(e.at) }),
      el("b", { text: words(e.event).toLowerCase() }),
      e.detail && el("small", { class: "muted", text: " " + Object.entries(e.detail).filter(([k]) => !["order_id", "visit"].includes(k)).map(([k, v]) => `${k}: ${typeof v === "number" && /amount|total/.test(k) ? money(v) : JSON.stringify(v)}`).join(" · ") }))));
  }

  function agentSide(side) {
    if (!side) return el("p", { class: "muted", text: "Placed on the web: no agent involved." });
    const out = [el("p", { class: "muted" }, "Agent: ", el("code", { text: side.profile ?? "?" }), side.known === false ? " · not one of ours; it can't be asked" : "")];
    const evidence = side.evidence;
    if (evidence?.findings) {
      out.push(el("ul", { class: "findings" }, evidence.findings.map((f) => el("li", { class: f.ok === true ? "yes" : f.ok === false ? "no" : "note" },
        el("span", { text: f.ok === true ? "✓" : f.ok === false ? "✕" : "–" }), el("span", { text: f.text })))));
    } else {
      out.push(el("p", { class: "muted", text: "The agent could not be asked for its record." }));
    }
    const chat = side.conversation?.events;
    if (chat) {
      out.push(el("h4", { text: `The conversation · ${side.conversation.brain}` }));
      const steps = chat.filter((e) => e.type === "activity");
      out.push(el("div", { class: "chat" }, chat.map((e) => {
        if (e.type === "person") return el("div", { class: "person", text: e.text });
        if (e.type === "agent") return el("div", { class: "agent", text: e.text });
        if (e.type === "proposal") return el("div", { class: "card-line" }, el("b", { text: `Proposed: ${e.proposal.title}` }), ` · ${e.proposal.shop_name} · card pays ${money(e.proposal.total)}`, e.proposal.previous_total != null && ` (was ${money(e.proposal.previous_total)}, the shop changed it)`, el("small", { class: "muted", text: e.proposal.reason }));
        if (e.type === "proposal_status") return el("div", { class: "step", text: `→ ${e.status}` });
        if (e.type === "receipt") return el("div", { class: "card-line" }, el("b", { text: "Bought" }), ` · ${e.receipt.codes.join(", ")} · charged ${money(e.receipt.charged ?? 0)}`);
        return null;
      })));
      out.push(el("p", { class: "muted", text: `${steps.length} signed requests to the shops in that conversation.` }));
    }
    return out;
  }

  let openShop = null;
  async function openOrder(id, shopId) {
    const first = selected === null;
    selected = id;
    openShop = shopId;
    drawOrders();
    const panel = $("#detail");
    const split = $(".split");
    panel.hidden = false;
    // Two frames so the panel starts from its closed width and slides open.
    if (first) await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    split.classList.add("open");
    // On a narrow screen the panel sits under the table: bring it into view.
    if (matchMedia("(max-width: 900px)").matches) setTimeout(() => panel.scrollIntoView({ block: "start", behavior: "smooth" }), 50);
    panel.replaceChildren(el("div", { class: "panel-body in skeleton", "aria-busy": "true" },
      el("div", { class: "sk", style: "width:40%" }), el("div", { class: "sk big", style: "width:70%" }), el("div", { class: "sk", style: "width:55%" }),
      el("div", { class: "sk strip-sk" }), el("div", { class: "sk", style: "width:30%" }), el("div", { class: "sk", style: "width:85%" }), el("div", { class: "sk", style: "width:60%" })));
    try {
      const order = await api(`/orders/${id}`, {}, shopId);
      if (selected !== id) return;  // Another order was opened meanwhile.
      const s = order.summary;
      const body = el("div", { class: "panel-body" },
        el("header", { class: "panel-head" },
          el("div", {},
            el("p", { class: "eyebrow" }, logo(s.shop, "small"), ` ${s.shop_name} · ${when(s.placed_at)}`),
            el("h3", { text: s.items.map((i) => `${i.quantity > 1 ? i.quantity + " × " : ""}${i.title}`).join(", ") }),
            el("p", { class: "muted who", text: `${s.buyer.full_name ?? ""} · ${s.buyer.email ?? ""}` }),
            badges(order)),
          el("div", { class: "head-tools" },
            markButton(order),
            el("button", { class: "close", type: "button", "aria-label": "Close", text: "×", onclick: closeOrder }))),
        el("div", { class: "strip" },
          el("div", {}, el("span", { text: "Card" }), el("b", { text: money(s.payment.amount) })),
          el("div", {}, el("span", { text: "Coins" }), el("b", { text: s.payment.coins ? `${s.payment.coins} used` : "none" }), s.payment.coins_earned ? el("small", { text: `${s.payment.coins_earned} earned` }) : null),
          el("div", {}, el("span", { text: "Rail" }), el("b", { text: s.payment.rail ?? "–" }), order.payment?.mode && el("small", { text: order.payment.mode })),
          el("div", {}, el("span", { text: "Door" }), el("b", { text: s.channel === "agent" ? "Agent" : "Web" }), s.channel === "agent" ? el("small", { text: `signature ${s.signature ?? "?"}` }) : null)),
        part("Payment", true, paymentPanel(order)),
        part("Merchant actions", true, ...actions(order)),
        part("Vouchers", true, vouchers(order)),
        part("Order facts", false, facts(order)),
        part(`The shop's events · ${order.events.length}`, false, timeline(order.events)),
        part("The agent's side", s.channel === "agent", ...[agentSide(order.agent)].flat()));
      panel.replaceChildren(body);
      requestAnimationFrame(() => body.classList.add("in"));
      // Opening it took it out of "new": show that in the list too.
      const row = orders.find((o) => o.id === id);
      if (row && row.attention !== s.attention) { row.attention = s.attention; drawOrders(); updateBadgeFromList(); }
    } catch (error) {
      panel.replaceChildren(el("div", { class: "panel-body in" }, el("p", { class: "muted", text: error.message })));
    }
  }

  function closeOrder() {
    selected = null;
    const panel = $("#detail");
    const split = $(".split");
    split.classList.remove("open");
    // Hide once the slide has finished, so the rows widen while it goes.
    const done = () => { if (!split.classList.contains("open")) panel.hidden = true; panel.removeEventListener("transitionend", done); };
    panel.addEventListener("transitionend", done);
    setTimeout(done, 450);
    drawOrders();
  }

  // ---- Events, inventory

  function drawEvents() {
    const query = $("#event-search").value.trim().toLowerCase();
    const rows = events.filter((e) => !query || JSON.stringify(e).toLowerCase().includes(query));
    $("#event-count").textContent = `${rows.length} of ${events.length} events`;
    $("#events tbody").replaceChildren(...rows.map((e) => el("tr", { class: e.event === "PAYMENT_DECLINED" ? "bad" : "" },
      el("td", { text: when(e.at) }),
      el("td", { text: e.seller }),
      el("td", {}, el("b", { text: words(e.event).toLowerCase() }), e.rail && el("small", { class: "mono", text: e.rail })),
      el("td", {}, el("code", { text: shortId(e.checkout_id) })),
      el("td", {}, el("small", { text: e.detail ? Object.entries(e.detail).map(([k, v]) => `${k}: ${typeof v === "number" && /amount|total/.test(k) ? money(v) : typeof v === "string" ? v : JSON.stringify(v)}`).join(" · ") : "" })))));
  }

  async function loadEvents() {
    loading("#event-count", "#events tbody", 5);
    try {
      events = await api("/events");
      drawEvents();
    } catch (error) { status(error.message, "bad"); }
  }

  async function loadInventory() {
    loading("#inventory-count", "#inventory tbody", 6);
    try {
      const rows = await api("/inventory");
      const all = shop === "all";
      const left = rows.reduce((n, r) => n + r.codes.available, 0);
      $("#inventory-count").textContent = `${rows.length} options · ${left} places left`;
      $("#inventory tbody").replaceChildren(...rows.map((r) => el("tr", { class: r.codes.available ? "" : "out" },
        el("td", {}, all && [logo(r.shop, "small"), ` ${r.shop_name} · `], `${r.deal} · ${r.option}`, el("small", { class: "mono", text: r.option_id })),
        el("td", { class: "num", text: r.codes.available }), el("td", { class: "num", text: r.codes.assigned }), el("td", { class: "num", text: r.codes.redeemed }), el("td", { class: "num", text: r.codes.void }), el("td", { class: "num", text: r.codes.total }))));
    } catch (error) { status(error.message, "bad"); }
  }

  // ---- Customers and demo forms

  function form(id, handler) {
    $(id).addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.target));
      try { await handler(data, event.target); } catch (error) { status(error.message, "bad"); }
    });
  }
  form("#wallet-form", async ({ email }) => {
    const w = await api(`/wallets/${encodeURIComponent(email)}`);
    $("#wallet-result").replaceChildren(
      el("p", { class: "big", text: `${w.balance} coins` }),
      el("ol", { class: "timeline" }, (w.entries ?? w.movements ?? []).map((m) => el("li", {}, el("time", { text: when(m.at ?? m.created_at) }), `${m.coins > 0 ? "+" : ""}${m.coins} · ${m.reason}`, m.order_id && el("small", { class: "muted", text: ` · order ${shortId(m.order_id)}` })))));
  });
  form("#grant-form", async ({ email, coins }, f) => { await api("/wallets/grant", { method: "POST", body: { email, coins: Number(coins) } }); status(`Granted ${coins} coins to ${email}.`, "good"); f.reset(); });
  form("#account-form", async (data, f) => { await api("/accounts", { method: "POST", body: data }); status(`Account created for ${data.email}.`, "good"); f.reset(); });
  form("#password-form", async ({ email, password }, f) => { await api(`/accounts/${encodeURIComponent(email)}/password`, { method: "PUT", body: { password } }); status(`Password set for ${email}.`, "good"); f.reset(); });
  form("#fee-form", async ({ amount }) => { await api("/booking-fee", { method: "POST", body: { amount: Number(amount) } }); status(`Booking fee set to ${money(Number(amount))}.`, "good"); });
  $("[data-clear-fee]").addEventListener("click", async () => { try { await api("/booking-fee", { method: "POST", body: { amount: 0 } }); status("Booking fee removed.", "good"); } catch (error) { status(error.message, "bad"); } });

  // ---- Navigation

  const loaders = { overview: loadStats, orders: loadOrders, events: loadEvents, inventory: loadInventory };

  async function refresh(keepOrder = false) {
    await (loaders[section] ?? (async () => {}))();
    if (keepOrder && selected && section === "orders") await openOrder(selected, openShop);
  }

  function showSection(name) {
    section = name;
    for (const node of $$(".section")) node.hidden = node.dataset.section !== name;
    for (const button of $$("#nav button")) button.classList.toggle("on", button.dataset.section === name);
    $("[data-section-name]").textContent = $(`#nav [data-section="${name}"]`).firstChild.textContent.trim();
    refresh();
  }

  function selectShop(id) {
    shop = id;
    const current = id === "all" ? ALL : shops.find((s) => s.id === id);
    for (const button of $$("#shops button")) button.classList.toggle("on", button.dataset.shop === id);
    $("[data-shop-name]").textContent = current.name;
    // Accounts, wallets and the fee belong to one shop at a time.
    for (const button of $$("#nav [data-one-shop]")) button.disabled = id === "all";
    closeOrder();
    try { localStorage.setItem("console.shop", id); } catch {}
    if (id === "all" && ["customers", "demo"].includes(section)) showSection("overview");
    else refresh();
  }

  $("#nav").addEventListener("click", (e) => { const b = e.target.closest("[data-section]"); if (b && !b.disabled) showSection(b.dataset.section); });
  $("#order-search").addEventListener("input", drawOrders);
  $("#event-search").addEventListener("input", drawEvents);
  $("#chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    const group = chip.closest(".chip-group");
    filters[group.dataset.filter] = chip.dataset.value;
    for (const c of $$(".chip", group)) c.classList.toggle("on", c === chip);
    drawOrders();
  });
  $("[data-export]").addEventListener("click", exportCsv);
  for (const b of $$("[data-refresh]")) b.addEventListener("click", () => refresh(true));

  (async () => {
    const data = await (await fetch("/api/shops")).json();
    shops = data.shops;
    baseTitle = data.name;
    document.title = data.name;
    $("[data-name]").textContent = data.name;
    $("#shops").replaceChildren(
      el("button", { type: "button", role: "tab", "data-shop": "all", onclick: () => selectShop("all") }, el("span", { class: "dot all" }), ALL.name, el("span", { class: "badge", hidden: true })),
      ...shops.map((s) => el("button", { type: "button", role: "tab", "data-shop": s.id, onclick: () => selectShop(s.id) }, logo(s.id), s.name, el("span", { class: "badge", hidden: true }))));
    let remembered = null;
    try { remembered = localStorage.getItem("console.shop"); } catch {}
    selectShop(remembered === "all" || shops.some((s) => s.id === remembered) ? remembered : "all");
    watch();
    setInterval(watch, 30000);
  })();
  // Who is signed in, and the way out. An app running open shows nothing.
  fetch("/api/whoami").then((r) => r.json()).then((me) => {
    const who = document.getElementById("who");
    if (!who || !me.user) return;
    who.querySelector("[data-user]").textContent = me.user;
    who.hidden = false;
  }).catch(() => {});
})();
