// The evidence page: draws /api/evidence/<order> as findings, a timeline and facts.
(() => {
  "use strict";
  const $ = (selector, root = document) => root.querySelector(selector);
  const orderId = decodeURIComponent(location.pathname.split("/").pop());

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value == null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    node.append(...children.flat(Infinity).filter((child) => child != null && child !== false));
    return node;
  }

  const money = (cents) => {
    const whole = Math.floor(Math.abs(cents) / 100);
    const rest = Math.abs(cents) % 100;
    return `${cents < 0 ? "-" : ""}$${whole}${rest ? "." + String(rest).padStart(2, "0") : ""}`;
  };
  const when = (iso) => iso ? new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "medium", timeZone: "UTC" }).format(new Date(iso)) + " UTC" : "";

  const EVENTS = {
    approved: (line) => [`Approved ${money(line.total)}`, `${line.by}, by ${line.method} · ${line.item}`],
    stopped: (line) => ["Purchase stopped, nothing paid", line.why === "total_changed" ? "The shop changed the total after the approval" : `Shop refused: ${line.why}`],
    purchased: (line) => [`Bought, shop charged ${money(line.charged ?? 0)}`, `Order ${line.order_id}`],
    declined: (line) => ["Declined by the customer", line.item],
  };

  function facts(data) {
    const order = data.order;
    const rows = [];
    if (!order) {
      rows.push(["Order", "The shop didn't give it"]);
      return rows;
    }
    rows.push(["Placed", when(order.placed_at) || "not recorded"]);
    rows.push(["Door", order.channel === "web" ? "The people's web checkout" : "The agent door (UCP)"]);
    if (order.agent) rows.push(["Agent", el("code", { text: order.agent })]);
    const signature = order.signature;
    if (signature) {
      const text = { verified: `Verified (key ${signature.keyid})`, failed: `Failed: ${signature.message ?? signature.code}`, missing: "None sent" }[signature.status] ?? signature.status;
      rows.push(["Signature", el("span", { class: signature.status === "verified" ? "ok" : "bad", text })]);
    }
    if (order.buyer) rows.push(["Buyer", `${order.buyer.full_name ?? ""} <${order.buyer.email ?? ""}>`]);
    const payment = order.payment ?? {};
    rows.push(["Charged", `${money(payment.amount ?? 0)} on the card (${payment.rail ?? "?"}, ${payment.status ?? "?"})` + (payment.coins ? ` + ${payment.coins} coins` : "")]);
    if (payment.payment_id) rows.push(["Payment", el("code", { text: payment.payment_id })]);
    for (const item of order.items ?? []) {
      rows.push(["Item", item.title]);
      rows.push(["Voucher", `${item.codes.join(", ") || "no code"} · ${item.voucher ?? ""} · ${item.redemption ?? ""}`]);
    }
    if (order.visit) rows.push(["Web hints", [order.visit.agent && `declared as ${order.visit.agent}`, ...(order.visit.hints ?? [])].filter(Boolean).join(", ") || "none"]);
    return rows;
  }

  (async () => {
    $("[data-order]").textContent = orderId;
    document.title = `Evidence · ${orderId.slice(0, 8)}`;
    const response = await fetch(`/api/evidence/${encodeURIComponent(orderId)}`);
    const data = await response.json();

    $("[data-findings]").replaceChildren(...data.findings.map((finding) =>
      el("li", { class: finding.ok === true ? "yes" : finding.ok === false ? "no" : "note" },
        el("span", { class: "mark", text: finding.ok === true ? "✓" : finding.ok === false ? "✕" : "–" }),
        el("span", { text: finding.text }))));

    const record = $("[data-record]");
    if (!data.record.length) record.replaceChildren(el("li", { class: "muted", text: "Nothing on record for this order." }));
    else record.replaceChildren(...data.record.map((line) => {
      const [title, detail] = (EVENTS[line.event] ?? ((l) => [l.event, ""]))(line);
      return el("li", {}, el("time", { text: when(line.at) }), el("b", { text: title }), detail && el("small", { text: detail }));
    }));

    $("[data-shop]").textContent = data.shop ? `${data.shop.name} · ${data.shop.url}` : "No shop could be asked.";
    $("[data-order-facts]").replaceChildren(...facts(data).flatMap(([label, value]) => [el("dt", { text: label }), el("dd", {}, value)]));
  })();
})();
