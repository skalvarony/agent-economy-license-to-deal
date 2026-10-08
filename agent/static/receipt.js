// The receipt page: one purchase, from /api/purchases/<order>.
(() => {
  "use strict";
  const $ = (selector, root = document) => root.querySelector(selector);
  const orderId = decodeURIComponent(location.pathname.split("/").pop());
  const el = (tag, props = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) { if (v == null || v === false) continue; if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v === true ? "" : v); }
    node.append(...children.flat(Infinity).filter((c) => c != null && c !== false));
    return node;
  };
  const money = (cents) => `$${Math.floor(Math.abs(cents) / 100)}${Math.abs(cents) % 100 ? "." + String(Math.abs(cents) % 100).padStart(2, "0") : ""}`;
  const when = (iso) => new Intl.DateTimeFormat("en-GB", { dateStyle: "long", timeStyle: "short" }).format(new Date(iso));

  (async () => {
    const response = await fetch(`/api/purchases/${encodeURIComponent(orderId)}`);
    if (!response.ok) { $("[data-shop]").textContent = "Not a purchase of this agent"; return; }
    const p = await response.json();
    document.title = `Receipt · ${p.shop_name} · ${p.order_id.slice(0, 8)}`;
    $("[data-shop]").textContent = p.shop_name;
    $("[data-shop-url]").textContent = p.shop_url ? new URL(p.shop_url).host : "";
    $("[data-order]").textContent = p.order_id;
    $("[data-date]").textContent = when(p.at);
    $("[data-buyer]").textContent = p.buyer ? `${p.buyer.full_name ?? ""} <${p.buyer.email ?? ""}>` : "";
    $("[data-agent]").textContent = "your agent, with your approval";

    const price = (p.charged ?? 0) + (p.coins ?? 0) * 100;
    $("[data-lines]").replaceChildren(el("tr", {},
      el("td", {}, p.title, p.service?.merchant && el("small", { text: `${p.service.merchant}${p.service.location ? ", " + p.service.location : ""}` })),
      el("td", {}, ...(p.codes.length ? p.codes.map((c) => el("code", { text: c })) : ["—"])),
      el("td", { class: "num", text: money(price) })));
    const rows = [];
    if (p.coins) rows.push(["Paid with coins", `−${money(p.coins * 100)}`]);
    rows.push(["Charged to the card" + (p.rail === "mock" ? " (simulated payment)" : ""), money(p.charged ?? 0)]);
    if (p.payment_id) rows.push(["Payment reference", p.payment_id]);
    if (p.coins_earned) rows.push(["Coins earned", `+${p.coins_earned}`]);
    if (p.payment === "refunded") rows.push(["Status", "Refunded"]);
    $("[data-totals]").replaceChildren(...rows.map(([k, v], i) => el("tr", { class: i === (p.coins ? 1 : 0) ? "total" : "" }, el("td", { colspan: 2, text: k }), el("td", { class: "num", text: v }))));

    const terms = [];
    const w = p.service?.window;
    if (w) terms.push(["When", `${when(w.not_before)} to ${new Intl.DateTimeFormat("en-GB", { timeStyle: "short" }).format(new Date(w.not_after))}`]);
    if (p.cancellation?.refundability) terms.push(["Cancellation", p.cancellation.refundability.replaceAll("_", " ") + (p.cancellation.refundable_until ? `, until ${when(p.cancellation.refundable_until)}` : "")]);
    if (p.voucher_terms?.expires_at) terms.push(["Valid until", when(p.voucher_terms.expires_at)]);
    if (p.redemption_terms?.instructions) terms.push(["To use it", p.redemption_terms.instructions]);
    terms.push(["State today", `${p.voucher ?? ""}${p.redemption ? " · " + p.redemption.replaceAll("_", " ") : ""}`]);
    $("[data-terms]").replaceChildren(...terms.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
    $("[data-footer]").textContent = `Issued by ${p.shop_name}; kept by your agent. ${p.rail === "mock" ? "The payment is simulated: no money moved. " : ""}This is a receipt for a voucher, not a tax invoice.`;
  })();
})();
