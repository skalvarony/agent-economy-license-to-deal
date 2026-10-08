// The venue simulator: the purchases that will show up at the door, and what
// happens to each of them.
(() => {
  "use strict";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  let world = null;       // every shop, its deals, its purchases
  let log = [];           // what happened, newest first
  let venue = "all";      // the merchant whose door this is, or every one
  let selected = null;    // the open purchase's order id
  const filters = { state: "open", shop: "all", door: "all" };
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
  const money = (cents) => `$${Math.floor(cents / 100)}${cents % 100 ? "." + String(cents % 100).padStart(2, "0") : ""}`;
  const fmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const when = (iso) => (iso ? fmt.format(new Date(iso)) : "–");
  const shortId = (id) => (id ? id.slice(0, 8) : "");
  const pill = (text, kind) => el("span", { class: `pill ${kind}`, text });
  const logo = (shopId, cls = "") => el("img", { class: `logo ${cls}`, src: `/api/${shopId}/logo.svg`, alt: "" });
  const words = (s) => (s ?? "").replaceAll("_", " ");
  const percent = (part, whole) => (whole ? `${Math.round((part / whole) * 100)}%` : "–");

  function status(text, kind = "", ms = 6000) {
    const node = $("#status");
    node.textContent = text;
    node.className = `status ${kind}`;
    if (text) setTimeout(() => { if (node.textContent === text) node.textContent = ""; }, ms);
  }

  function copyButton(text) {
    return el("button", { class: "copy", type: "button", title: "Copy", "aria-label": "Copy", text: "⧉", onclick: async (e) => {
      e.stopPropagation();
      try { await navigator.clipboard.writeText(text); status("Copied.", "good"); } catch { status("Couldn't copy.", "bad"); }
    } });
  }

  // ---- The venues, and the purchases flattened with their shop and deal

  // The merchants behind the deals, each with where it is and what it sells.
  function venues() {
    const found = new Map();
    for (const s of world.shops) {
      for (const deal of s.deals) {
        const v = found.get(deal.merchant) ?? { name: deal.merchant, location: deal.location, deals: 0, shops: new Set() };
        v.deals++; v.shops.add(s.name);
        found.set(deal.merchant, v);
      }
    }
    return [...found.values()].sort((a, b) => a.name.localeCompare(b.name));
  }
  const mine = (deal) => venue === "all" || deal.merchant === venue;

  function purchases() {
    const out = [];
    for (const s of world.shops) {
      for (const deal of s.deals) {
        if (!mine(deal)) continue;
        for (const p of deal.purchases) out.push({ ...p, shop: s, deal });
      }
    }
    out.sort((a, b) => (b.placed_at ?? "").localeCompare(a.placed_at ?? ""));
    return out;
  }

  // The log entries that belong to this venue's purchases.
  function myLog() {
    if (venue === "all") return log;
    const ids = new Set(purchases().map((p) => p.order_id));
    return log.filter((e) => ids.has(e.order_id));
  }

  // open: the voucher can still be used; used; letdown: the venue failed the
  // customer (the refund follows); refunded: the shop gave the money back
  // for its own reasons, before any visit.
  function outcome(p) {
    if (p.redemption === "redeemed") return "used";
    if (p.redemption === "cancelled_by_merchant" || p.redemption === "redemption_failed") return "letdown";
    if (p.payment === "refunded") return "refunded";
    return "open";
  }
  const isOpen = (p) => outcome(p) === "open";

  function statePills(p) {
    const out = [];
    const o = outcome(p);
    if (o === "letdown") out.push(p.redemption === "redemption_failed" ? pill("turned away", "bad") : pill("cancelled by the venue", "bad"));
    else if (o === "refunded") out.push(pill("refunded by the shop", "bad"));
    else out.push(o === "used" ? pill("used", "good") : pill("open", "plain"));
    if (p.payment === "refunded") out.push(" ", pill("code void", "bad"));
    const noShows = log.filter((e) => e.order_id === p.order_id && e.what === "no_show").length;
    if (noShows && isOpen(p)) out.push(" ", pill(`no-show ×${noShows}`, "warn"));
    return out;
  }

  function flag(p) {
    const o = outcome(p);
    const title = { open: "Open: the customer can still come", used: "Used at the venue", letdown: "The venue let the customer down", refunded: "Refunded; the code is void" }[o];
    return el("span", { class: `mark ${o}`, title, "aria-label": title, text: o === "used" ? "✓" : o === "letdown" || o === "refunded" ? "✕" : "" });
  }

  // ---- Overview

  function kpi(label, value, note) {
    return el("div", { class: "kpi" }, el("span", { class: "label", text: label }), el("b", { text: value }), note && el("small", { text: note }));
  }
  function drawKpis() {
    const all = purchases();
    const happened = myLog();
    const by = (o) => all.filter((p) => outcome(p) === o).length;
    const open = by("open"), used = by("used"), letdown = by("letdown");
    const refused = happened.filter((e) => e.what === "refused").length;
    const cancelled = happened.filter((e) => e.what === "cancelled").length;
    const noShows = happened.filter((e) => e.what === "no_show").length;
    const refundedCents = all.filter((p) => p.payment === "refunded").reduce((n, p) => n + p.paid, 0);
    const goodwill = (refused + cancelled) * (world.goodwill_coins ?? 0);
    $("#kpis").replaceChildren(
      kpi("Still to come", open, "open vouchers"),
      kpi("Served", used, `${percent(used, all.length)} of ${all.length} sold`),
      kpi("Let down", letdown, `${refused} turned away · ${cancelled} cancelled`),
      kpi("No-shows", noShows, "the customer's, no refund"),
      kpi("Given back", money(refundedCents), `by the marketplaces · ${goodwill} goodwill coins`));
  }

  // ---- Arrivals

  function keep(p) {
    const o = outcome(p);
    if (filters.state !== "all" && o !== filters.state) return false;
    if (filters.shop !== "all" && p.shop.id !== filters.shop) return false;
    if (filters.door === "agent" && p.channel !== "agent") return false;
    if (filters.door === "web" && p.channel === "agent") return false;
    const q = $("#search").value.trim().toLowerCase();
    return !q || JSON.stringify({ b: p.buyer, t: p.title, c: p.codes, id: p.order_id, d: p.deal.title }).toLowerCase().includes(q);
  }

  function drawShopChips() {
    const group = $("#shop-chips");
    group.replaceChildren(
      el("button", { type: "button", class: `chip ${filters.shop === "all" ? "on" : ""}`, "data-value": "all", text: "Any marketplace" }),
      ...world.shops.map((s) => el("button", { type: "button", class: `chip ${filters.shop === s.id ? "on" : ""}`, "data-value": s.id }, logo(s.id, "small"), ` ${s.name}`)));
  }

  function drawArrivals() {
    const all = purchases();
    const rows = all.filter(keep);
    $("#count").textContent = `${rows.length} of ${all.length}`;
    const body = $("#arrivals tbody");
    body.replaceChildren(...rows.map((p) => el("tr", { class: `${p.order_id === selected ? "on" : ""} ${outcome(p)}`, onclick: () => openPurchase(p.order_id) },
      el("td", { class: "flag" }, flag(p)),
      el("td", {}, when(p.placed_at), el("small", { class: "mono", text: shortId(p.order_id) })),
      el("td", { class: "via" }, logo(p.shop.id, "small"), el("span", { class: "fold", text: " " + p.shop.name })),
      el("td", {}, p.buyer.full_name ?? "", el("small", { text: `${p.buyer.email ?? ""} · ${p.channel === "agent" ? "via agent" + (p.signature === "verified" ? ", signed" : "") : "on the web"}` })),
      el("td", { class: "bought" }, el("div", { text: p.deal.title, title: p.deal.title }), el("small", { text: `${p.option ?? p.title.split(" · ").pop()} · ${money(p.paid)}${p.coins ? ` + ${p.coins} coins` : ""}` })),
      el("td", {}, p.codes.map((c) => el("code", { class: "code", text: c }))),
      el("td", {}, statePills(p)))));
    if (!rows.length) body.replaceChildren(el("tr", {}, el("td", { colspan: 7, class: "empty", text: all.length ? "Nothing matches." : "No purchases yet." })));

  }

  // ---- One purchase: the panel where things happen

  const HAPPEN = [
    ["arrived", "Customer arrives, voucher honoured", "primary", "The voucher is used. The sale becomes final."],
    ["refused", "Customer arrives, venue can't honour it", "danger", "The marketplace refunds in full and adds goodwill coins."],
    ["cancelled", "Venue cancels the slot", "danger", "Same as a refusal: refund and goodwill."],
    ["no_show", "Customer never shows up", "secondary", "Nothing changes; the voucher stays valid. No refund."],
  ];

  function part(title, open, ...children) {
    return el("details", { class: "part", open }, el("summary", {}, el("h4", { text: title })), el("div", { class: "part-body" }, ...children));
  }

  function happenBox(p) {
    const open = isOpen(p);
    const note = el("input", { class: "note", name: "note", placeholder: "A note for the record (optional)", "aria-label": "Note" });
    const outcomeBox = el("div", { class: "outcome-box" });
    const buttons = HAPPEN.map(([what, label, kind, hint]) =>
      el("button", { type: "button", class: `happen ${kind}`, disabled: !open, onclick: () => happen(p, what, note.value, outcomeBox) },
        el("b", { text: label }), el("small", { text: hint })));
    return el("div", { class: "happenings" },
      open ? null : el("p", { class: "muted closed", text: "This voucher is closed: nothing more can happen to it at the door." }),
      open ? note : null,
      el("div", { class: "happen-grid" }, buttons),
      outcomeBox);
  }

  async function happen(p, what, note, box) {
    box.replaceChildren(el("p", { class: "muted", text: `${world.happenings[what]}…` }));
    try {
      const response = await fetch(`/api/${p.shop.id}/orders/${p.order_id}/happen`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ what, note }),
      });
      const entry = await response.json();
      if (!response.ok) throw new Error(entry.detail ?? `HTTP ${response.status}`);
      status(`${entry.label}: recorded.`, "good");
      await load(true);
      // The panel reloads with the new state; the outcome is shown in its history.
    } catch (error) {
      box.replaceChildren(el("p", { class: "status bad", text: error.message }));
    }
  }

  function history(p) {
    const entries = log.filter((e) => e.order_id === p.order_id);
    if (!entries.length) return el("p", { class: "muted", text: "Nothing has happened at the door yet." });
    return el("ol", { class: "timeline" }, entries.slice().reverse().map((e) => el("li", { class: e.what === "arrived" ? "" : e.what === "no_show" ? "" : "bad" },
      el("time", { text: when(e.at) }),
      el("b", { text: e.label }),
      e.note && el("div", { class: "muted", text: `“${e.note}”` }),
      el("ul", { class: "consequences" }, e.consequences.map((c) => el("li", { text: c }))))));
  }

  function terms(p) {
    const rows = [];
    if (p.booking?.starts_at) rows.push(["Booked for", when(p.booking.starts_at) + (p.booking.status === "released" ? " (released)" : "")]);
    if (p.window?.not_before) rows.push(["Service window", `${when(p.window.not_before)} → ${when(p.window.not_after)}`]);
    if (p.cancellation?.refundability) rows.push(["Cancellation", words(p.cancellation.refundability) + (p.cancellation.refundable_until ? ` until ${when(p.cancellation.refundable_until)}` : "")]);
    if (p.expires_at) rows.push(["Voucher expires", when(p.expires_at)]);
    if (p.redemption_method) rows.push(["How to redeem", words(p.redemption_method)]);
    if (p.instructions) rows.push(["Instructions", p.instructions]);
    rows.push(["Paid", `${money(p.paid)} on the card (${p.rail ?? "?"})${p.coins ? ` + ${p.coins} coins` : ""}`]);
    rows.push(["Door", p.channel === "agent" ? `Agent, signature ${p.signature ?? "?"}` : "Web"]);
    rows.push(["Order", el("span", { class: "with-copy" }, el("code", { text: p.order_id }), copyButton(p.order_id))]);
    return el("dl", { class: "facts" }, rows.map(([k, v]) => [el("dt", { text: k }), el("dd", {}, v)]));
  }

  async function openPurchase(id) {
    const first = selected === null;
    selected = id;
    drawArrivals();
    const p = purchases().find((x) => x.order_id === id);
    const panel = $("#detail");
    const split = $(".split");
    panel.hidden = false;
    if (first) await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    split.classList.add("open");
    // On a narrow screen the panel sits under the table: bring it into view.
    if (matchMedia("(max-width: 900px)").matches) setTimeout(() => panel.scrollIntoView({ block: "start", behavior: "smooth" }), 50);
    if (!p) { panel.replaceChildren(el("div", { class: "panel-body in" }, el("p", { class: "muted", text: "That purchase is not in view." }))); return; }
    const body = el("div", { class: "panel-body" },
      el("header", { class: "panel-head" },
        el("div", {},
          el("p", { class: "eyebrow" }, `${p.deal.merchant}${p.deal.location ? " · " + p.deal.location : ""} · sold via `, logo(p.shop.id, "small"), ` ${p.shop.name}`),
          el("h3", { text: `${p.deal.title} · ${p.option ?? p.title.split(" · ").pop()}` }),
          el("p", { class: "muted who", text: `${p.buyer.full_name ?? ""} · ${p.buyer.email ?? ""} · bought ${when(p.placed_at)}` }),
          el("p", { class: "badges" }, statePills(p))),
        el("button", { class: "close", type: "button", "aria-label": "Close", text: "×", onclick: closePurchase })),
      el("div", { class: "ticket" },
        el("span", { class: "label", text: p.codes.length > 1 ? "Codes to show at the door" : "Code to show at the door" }),
        el("div", { class: "codes" }, p.codes.map((c) => el("span", { class: "with-copy" }, el("code", { text: c }), copyButton(c))))),
      part("What happens at the door", true, happenBox(p)),
      part("What has happened", true, history(p)),
      part("Terms of the voucher", false, terms(p)));
    panel.replaceChildren(body);
    requestAnimationFrame(() => body.classList.add("in"));
  }

  function closePurchase() {
    selected = null;
    const panel = $("#detail");
    const split = $(".split");
    split.classList.remove("open");
    const done = () => { if (!split.classList.contains("open")) panel.hidden = true; panel.removeEventListener("transitionend", done); };
    panel.addEventListener("transitionend", done);
    setTimeout(done, 450);
    drawArrivals();
  }

  // ---- Loading and navigation

  function drawAll() { drawKpis(); drawShopChips(); drawArrivals(); }
  function syncChips() { for (const g of $$(".chip-group")) for (const c of $$(".chip", g)) c.classList.toggle("on", c.dataset.value === filters[g.dataset.filter]); }

  async function load(keepOpen = false) {
    try {
      const [w, l] = await Promise.all([fetch("/api/world"), fetch("/api/log")]);
      world = await w.json();
      log = await l.json();
      baseTitle = world.name;
      $("[data-name]").textContent = world.name;
      const list = venues();
      $("#venues").replaceChildren(
        el("button", { type: "button", role: "tab", "data-venue": "all", onclick: () => selectVenue("all") }, el("span", { class: "dot all" }), "All venues"),
        ...list.map((v) => el("button", { type: "button", role: "tab", "data-venue": v.name, title: `${v.location ?? ""} · ${v.deals} deal${v.deals === 1 ? "" : "s"} via ${[...v.shops].join(", ")}`, onclick: () => selectVenue(v.name) }, el("span", { class: "dot venue" }), v.name)));
      if (venue !== "all" && !list.some((v) => v.name === venue)) venue = "all";
      for (const b of $$("#venues button")) b.classList.toggle("on", b.dataset.venue === venue);
      $("[data-venue-name]").textContent = venue === "all" ? "All venues" : venue;
      for (const s of world.shops) if (s.error) status(`${s.name}: ${s.error}`, "bad");
      drawAll();
      const open = purchases().filter(isOpen).length;
      document.title = (open ? `(${open}) ` : "") + baseTitle;
      if (keepOpen && selected) await openPurchase(selected);
    } catch (error) {
      status(error.message, "bad");
    }
  }

  function selectVenue(name) {
    venue = name;
    for (const b of $$("#venues button")) b.classList.toggle("on", b.dataset.venue === name);
    $("[data-venue-name]").textContent = name === "all" ? "All venues" : name;
    try { localStorage.setItem("venue.venue", name); } catch {}
    closePurchase();
    drawAll();
    const open = purchases().filter(isOpen).length;
    document.title = (open ? `(${open}) ` : "") + baseTitle;
  }

  $("#search").addEventListener("input", drawArrivals);
  $("#chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    const group = chip.closest(".chip-group");
    filters[group.dataset.filter] = chip.dataset.value;
    for (const c of $$(".chip", group)) c.classList.toggle("on", c === chip);
    drawArrivals();
  });
  for (const b of $$("[data-refresh]")) b.addEventListener("click", () => load(true));

  try { const remembered = localStorage.getItem("venue.venue"); if (remembered) venue = remembered; } catch {}
  load();
  setInterval(() => load(true), 30000);
  // Who is signed in, and the way out. An app running open shows nothing.
  fetch("/api/whoami").then((r) => r.json()).then((me) => {
    const who = document.getElementById("who");
    if (!who || !me.user) return;
    who.querySelector("[data-user]").textContent = me.user;
    who.hidden = false;
  }).catch(() => {});
})();
