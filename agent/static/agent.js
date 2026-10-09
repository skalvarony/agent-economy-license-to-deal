// The agent's page. It draws the events the server sends: what was said, each
// request to a shop, each proposal and each receipt. Plain DOM, no build step.
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const talk = $("#talk");
  const feed = $("#feed");
  const shops = new Map();
  let lastSeq = -1;
  let busy = false;
  let working = null;
  // The requests of the turn in progress: one line that opens into a list.
  let steps = null;

  // ---- Small helpers

  // Build an element. Text always goes in as text, never as HTML.
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value == null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "shop") node.style.setProperty("--shop", shopColor(value));
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    node.append(...children.flat(Infinity).filter((child) => child != null && child !== false));
    return node;
  }

  const shopColor = (id) => shops.get(id)?.color ?? "currentColor";
  // A shop's mark: its logo, or a dot in its colour until the logo is known.
  const dot = (id) => {
    const logo = shops.get(id)?.logo;
    return logo ? el("img", { class: "dot logo", src: logo, alt: "" }) : el("span", { class: "dot", shop: id });
  };

  const whenFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const when = (iso) => (iso ? whenFmt.format(new Date(iso)) : "");

  function money(cents) {
    const whole = Math.floor(Math.abs(cents) / 100);
    const rest = Math.abs(cents) % 100;
    return `${cents < 0 ? "-" : ""}$${whole}${rest ? "." + String(rest).padStart(2, "0") : ""}`;
  }

  const dayTime = new Intl.DateTimeFormat("en-GB", {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
  const dayOnly = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  const timeOnly = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
  const moment = (iso) => dayTime.format(new Date(iso));
  const coinsText = (n) => `${n} ${n === 1 ? "coin" : "coins"}`;

  // The first thing said moves the composer from the middle to the bottom.
  let begun = false;
  function begin() {
    if (begun || !talk.classList.contains("empty")) return;
    begun = true;   // a transition starts async; many adds in one tick must not start many
    const start = () => talk.classList.remove("empty");
    if (document.startViewTransition && lastSeq >= 0) document.startViewTransition(start);
    else start();
  }

  // Add to the thread, keeping the "working" dot last. While a page of the
  // thread is being drawn, nodes go to `target` (a fragment) and nothing
  // scrolls; the caller puts the fragment in place.
  let target = null;
  function add(node) {
    if (target) { target.append(node); return node; }
    begin();
    feed.insertBefore(node, working);
    (working ?? node).scrollIntoView({ block: "end", behavior: "smooth" });
    return node;
  }

  // Where a message came from, when not from this page: a small mark.
  function chan(via, role) {
    if (!via) return null;
    const key = sourceOf(via);
    const label = SOURCES[key]?.label ?? via;
    const where = key === "telegram" || key === "voice" ? "on" : "in";
    const text = role === "person" ? `You, ${where} ${label}` : `${where} ${label}`;
    return el("small", { class: "chan" }, sourceIcon(key), el("span", { text }));
  }

  // A day's first entry gets the day above it.
  let lastDay = null;
  function daySep(at) {
    const d = dayOf(at ?? new Date().toISOString());
    if (d === lastDay) return;
    lastDay = d;
    add(el("div", { class: "day-sep" }, el("span", { text: d })));
  }

  // ---- What a deal's terms say, in words

  function refundText(cancellation) {
    if (cancellation.refundability === "non_refundable") return "Non-refundable";
    const full = cancellation.refundability === "refundable" ? "Full refund" : "Partial refund";
    if (cancellation.refundable_until) return `${full} if you cancel by ${moment(cancellation.refundable_until)}`;
    if (cancellation.refund_days) return `${full} for ${cancellation.refund_days} days after buying`;
    return full;
  }

  // The slot a purchase is booked for, e.g. "Sat 10 Oct, 11:00 to 12:00".
  function bookedText(booking) {
    const end = booking.ends_at ? ` to ${timeOnly.format(new Date(booking.ends_at))}` : "";
    return `${moment(booking.starts_at)}${end}${booking.status === "released" ? " (released)" : ""}`;
  }

  function terms(deal) {
    const { service = {}, cancellation = {}, voucher = {}, redemption = {} } = deal;
    const rows = [];
    const window = service.window;
    const booking = service.booking;
    if (booking?.starts_at) {
      rows.push(["When", bookedText(booking)]);
    } else if (window) {
      rows.push(["When", `${moment(window.not_before)} to ${timeOnly.format(new Date(window.not_after))}`]);
    } else if (booking?.required) {
      rows.push(["When", "A date and time you pick when buying"]);
    } else {
      rows.push(["When", redemption.appointment_required ? "You book the date after buying" : "Any day while the voucher is valid"]);
    }
    if (service.merchant) rows.push(["Where", [service.merchant, service.location].filter(Boolean).join(", ")]);
    if (service.includes?.length) rows.push(["Includes", service.includes.join(", ")]);
    if (cancellation.refundability) rows.push(["Cancellation", refundText(cancellation)]);
    if (voucher.expires_at) rows.push(["Valid until", dayOnly.format(new Date(voucher.expires_at))]);
    else if (voucher.valid_days) rows.push(["Valid for", `${voucher.valid_days} days after buying`]);
    if (redemption.instructions) rows.push(["To use it", redemption.instructions]);
    return el("dl", { class: "terms" }, rows.map(([label, value]) => [el("dt", { text: label }), el("dd", { text: value })]));
  }

  // ---- Messages

  function message(role, text, via = null) {
    add(el("div", { class: `msg ${role}${via ? " elsewhere" : ""}` }, chan(via, role), el("p", { text })));
  }

  // ---- A proposal: what the agent would buy, waiting for a yes or a no

  const MARKS = { chosen: "✓", rejected: "✕", alternative: "·" };
  const SHORT = { approved: "Paying…", bought: "Bought", done: "Done", declined: "Declined", withdrawn: "Replaced", changed: "Total changed", failed: "Refused" };
  const STATES = {
    approved: "Approved. Paying the shop…",
    bought: "Bought, at the total you approved.",
    done: "Done, as you approved.",
    declined: "You turned this down. Nothing was paid.",
    withdrawn: "Replaced by a newer proposal. Nothing was paid.",
    changed: "The shop changed the total after you approved. Nothing was paid.",
    failed: "The shop refused the purchase. Nothing was paid.",
  };

  function compared(rows) {
    if (!rows.length) return null;
    return el("details", { class: "compared", open: true },
      el("summary", { text: `What I compared (${rows.length})` }),
      el("ul", {}, rows.map((row) => el("li", { class: row.verdict },
        el("span", { class: "mark", text: MARKS[row.verdict] ?? "·" }),
        el("span", { class: "what" },
          [row.shop_name, row.title].filter(Boolean).join(" · "),
          row.reason && el("small", { text: row.reason })),
        el("span", { class: "price", text: row.price == null ? "" : money(row.price) })))));
  }

  function sum(totals) {
    return el("dl", { class: "sum" }, totals.map((total) => {
      const last = total.type === "total";
      return [
        el("dt", { class: last && "total", text: total.label }),
        el("dd", { class: last && "total", text: money(total.amount) }),
      ];
    }));
  }

  const stars = (value) => (value ? "★".repeat(Math.round(value)) + "☆".repeat(5 - Math.round(value)) : "");

  function gallery(urls, one) {
    if (!urls?.length) return null;
    const box = el("div", { class: one ? "gallery one" : "gallery" }, urls.slice(0, 3).map((url) => el("img", { src: url, alt: "", loading: "lazy" })));
    // Clicking a photo brings it to the front.
    box.addEventListener("click", (e) => { if (e.target.tagName === "IMG" && box.firstChild !== e.target) box.prepend(e.target); });
    return box;
  }

  function leadPrice(proposal) {
    const option = proposal.deal?.option;
    const was = option?.list_price;
    return el("div", { class: "lead-price" },
      el("span", { class: "big", text: money(proposal.total) }),
      was && el("span", { class: "was", text: money(was) }),
      el("span", { class: "split", text: `your card · ${proposal.coins?.applied ? `${coinsText(proposal.coins.applied)} already taken off · ` : ""}${option ? money(option.price) + " list" : ""}` }));
  }

  function leadTerms(proposal) {
    const { service = {}, cancellation = {} } = proposal;
    const rows = [];
    if (service.booking?.starts_at) rows.push(["When", bookedText(service.booking)]);
    else if (service.window) rows.push(["When", `${moment(service.window.not_before)} to ${timeOnly.format(new Date(service.window.not_after))}`]);
    rows.push(["Cancellation", refundText(cancellation)]);
    if (service.merchant) rows.push(["Where", [service.merchant, service.location].filter(Boolean).join(", ")]);
    return el("dl", { class: "lead-terms" }, rows.map(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
  }

  // The three best, side by side: the proposed one marked, the others a
  // click away.
  function shortlist(proposal) {
    const rows = proposal.shortlist ?? [];
    if (rows.length < 2) return null;
    const chosenId = proposal.deal?.option?.id;
    const refund = (r) => r === "refundable" ? "Refundable" : r === "non_refundable" ? "Final sale" : "Partly refundable";
    return el("section", { class: "shortlist" },
      el("h4", { text: "The three best · pick the one you want" }),
      el("div", { class: "picks" }, rows.map((card) => {
        const chosen = card.option?.id === chosenId;
        const photo = card.gallery?.[0] ?? card.image;
        return el("article", { class: `pick-card ${chosen ? "chosen" : ""}` },
          photo ? el("img", { src: photo, alt: "" }) : el("div", { class: "noimg" }),
          el("div", { class: "body" },
            el("b", { text: card.title }),
            el("small", { text: `${card.option?.label ?? ""}${card.option?.label ? " · " : ""}${card.shop_name}` }),
            el("div", { class: "facts" },
              el("span", { class: "price", text: card.option ? money(card.option.price) : "" }),
              el("span", { class: `refund ${card.refundability === "refundable" ? "ok" : ""}`, text: refund(card.refundability) }),
              card.rating?.value && el("span", { class: "stars", text: `${stars(card.rating.value)} ${card.rating.value}` })),
            chosen
              ? el("span", { class: "proposed", text: "Proposed" })
              : el("button", { class: "pick", type: "button", text: "Pick this one", onclick: () => run(`/api/proposals/${proposal.id}/switch`, { shop: card.shop, option_id: card.option?.id }) })));
      })));
  }

  // A change to a purchase, waiting for a yes: move the visit, or cancel.
  function changeCard(proposal, into = null) {
    const cancel = proposal.kind === "cancel";
    const c = proposal.change ?? {};
    const booking = proposal.service?.booking;
    const head = el("div", { class: "head plain" },
      el("div", {},
        el("span", { class: "tag", "data-tag": true, text: "Waiting for your approval" }),
        proposal.via && el("span", { class: "via", text: `Proposed by ${proposal.via}${proposal.proposed_at ? " · " + when(proposal.proposed_at) : ""}` }),
        el("h3", { text: `${cancel ? "Cancel" : "Move"} ${proposal.title}` }),
        el("p", { class: "seller" }, dot(proposal.shop), ` ${proposal.shop_name}`),
        proposal.reason && el("p", { class: "why", text: proposal.reason })));
    const rows = cancel
      ? [["Back to your card", money(c.refund ?? 0)], c.coins ? ["Back to your wallet", coinsText(c.coins)] : null, booking?.starts_at ? ["Booked for", bookedText(booking)] : null]
      : [["From", moment(c.from)], ["To", moment(c.to)]];
    const card = el("article", { class: "card proposal change", "data-proposal": proposal.id },
      head,
      el("dl", { class: "terms" }, rows.filter(Boolean).map(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })])),
      el("div", { class: "pay" },
        el("div", { class: "actions" },
          el("button", { class: "primary", type: "button", "data-approve": true, text: cancel ? "Yes, cancel and refund" : "Yes, move it", onclick: () => run(`/api/proposals/${proposal.id}/approve`) }),
          el("button", { class: "secondary", type: "button", "data-decline": true, text: "Not this one", onclick: () => run(`/api/proposals/${proposal.id}/decline`) })),
        el("p", { class: "pledge", text: cancel ? "Nothing is cancelled until you approve." : "Nothing changes until you approve." })));
    if (into) into.append(card); else add(card);
    if (proposal.status !== "pending") proposalStatus(proposal.id, proposal.status);
    return card;
  }

  function proposalCard(proposal, into = null) {
    if (proposal.kind === "cancel" || proposal.kind === "reschedule") return changeCard(proposal, into);
    const service = proposal.service ?? {};
    const earns = proposal.coins?.earns;
    const show = proposal.presentation ?? {};
    const full = show.detail !== "brief";
    const deal = proposal.deal;
    const photos = deal?.gallery?.length ? deal.gallery : proposal.image ? [proposal.image] : [];
    const head = el("div", { class: "head plain" },
      el("div", {},
        el("span", { class: "tag", "data-tag": true, text: "Waiting for your approval" }),
        proposal.via && el("span", { class: "via", text: `Proposed by ${proposal.via}${proposal.proposed_at ? " · " + when(proposal.proposed_at) : ""}` }),
        el("h3", { text: proposal.title + (proposal.quantity > 1 ? ` × ${proposal.quantity}` : "") }),
        el("p", { class: "seller" }, dot(proposal.shop), ` ${proposal.shop_name}`, service.merchant ? ` · at ${service.merchant}` : "", deal?.rating?.value && el("span", { class: "stars", text: ` ${stars(deal.rating.value)} ${deal.rating.value}` })),
        proposal.reason && el("p", { class: "why", text: proposal.reason })));
    const lead = show.lead === "price" ? leadPrice(proposal) : show.lead === "terms" ? leadTerms(proposal) : gallery(photos, !full);
    const body = full ? [
      show.lead !== "photo" && gallery(photos, false),
      show.lead !== "terms" && terms(proposal),
      deal?.highlights?.length && el("ul", { class: "highlights" }, deal.highlights.map((h) => el("li", { text: h }))),
      deal?.reviews?.length && el("ul", { class: "reviews" }, deal.reviews.slice(0, 2).map((r) => el("li", {}, el("span", { class: "stars", text: stars(r.rating) }), ` ${r.text} `, el("span", { class: "who", text: `— ${r.author}` })))),
      deal?.about_merchant && el("p", { class: "about", text: deal.about_merchant }),
    ] : [show.lead !== "terms" && el("dl", { class: "terms" }, el("dt", { text: "Cancellation" }), el("dd", { text: refundText(proposal.cancellation ?? {}) }))];
    const card = el("article", { class: "card proposal" + (full ? "" : " brief"), "data-proposal": proposal.id },
      show.lead === "photo" ? [lead, head] : [head, lead],
      proposal.previous_total != null && el("p", { class: "changed" },
        "The shop changed the total after you approved: ",
        el("s", { text: money(proposal.previous_total) }), ` → ${money(proposal.total)}. Nothing was paid.`),
      ...body,
      show.shortlist === 3 ? shortlist(proposal) : null,
      full && show.shortlist !== 3 && compared(proposal.considered ?? []),
      el("div", { class: "pay" },
        sum(proposal.totals),
        earns > 0 && el("p", { class: "earn", text: `You'll earn ${coinsText(earns)} back at ${proposal.shop_name}.` }),
        el("div", { class: "actions" },
          el("button", {
            class: "primary", type: "button", "data-approve": true,
            text: `Approve ${money(proposal.total)}`,
            onclick: () => run(`/api/proposals/${proposal.id}/approve`),
          }),
          el("button", {
            class: "secondary", type: "button", "data-decline": true, text: "Not this one",
            onclick: () => run(`/api/proposals/${proposal.id}/decline`),
          })),
        el("p", { class: "pledge", text: "I can't pay without your approval, and it covers this total only." })));
    if (into) into.append(card); else add(card);
    if (proposal.status !== "pending") proposalStatus(proposal.id, proposal.status);
    return card;
  }

  // A proposal may be on the page twice (the chat and the approvals).
  function proposalStatus(id, status, method = null) {
    if (status === "pending") return;
    // The card may still be in the fragment of a page being drawn.
    const cards = [...document.querySelectorAll(`[data-proposal="${id}"]`), ...(target ? target.querySelectorAll(`[data-proposal="${id}"]`) : [])];
    for (const card of cards) {
      card.classList.add("closed");
      $("[data-tag]", card).textContent = "Proposal";
      $(".actions", card)?.remove();
      $(".pledge", card)?.remove();
      $(".compared", card)?.removeAttribute("open");
      const state = $(".state", card) ?? $(".pay", card).appendChild(el("p", { class: "state" }));
      state.className = `state ${status}`;
      state.replaceChildren(el("span", { text: STATES[status] ?? status }));
      // Where the yes or the no came from, when not this page.
      if (method && channelOf(method) !== "page") state.append(" ", channelBadge(method));
    }
    if (!target) refreshApprovals();
  }

  // ---- History: the agent's journal, by day, each entry in a side panel

  let historyKind = "all";
  let historySource = "all";
  let historyData = null;
  let historyOpen = null;   // key of the entry in the panel

  // Where something came from: the page, the agent's own brain, or another
  // assistant over MCP; the channel a decision came through.
  const SOURCES = {
    page:     { label: "Web",            icon: "page" },
    agent:    { label: "Your agent",     icon: "agent" },
    chatgpt:  { label: "ChatGPT",        icon: "openai" },
    codex:    { label: "Codex",          icon: "openai" },
    claude:   { label: "Claude",         icon: "claude" },
    desktop:  { label: "Claude Desktop", icon: "claude" },
    code:     { label: "Claude Code",    icon: "claude", mark: "code" },
    telegram: { label: "Telegram",       icon: "telegram" },
    voice:    { label: "Voice",          icon: "voice" },
    other:    { label: "An assistant",   icon: "mcp" },
  };
  function sourceOf(name) {
    const n = (name ?? "").toLowerCase();
    if (!n || n === "page" || n === "button") return "page";
    if (n.includes("claude-code") || n.includes("claude code")) return "code";
    // ChatGPT's connector calls itself "openai-mcp (Codex)".
    if (n.includes("chatgpt") || n.includes("openai")) return "chatgpt";
    if (n.includes("codex")) return "codex";
    if (n.includes("claude") && n.includes("desktop")) return "desktop";
    if (n.includes("claude")) return "claude";
    if (n.includes("telegram")) return "telegram";
    if (n.includes("voice") || n.includes("phone")) return "voice";
    return "other";
  }
  // A decision's method: "page", "card:ChatGPT", "telegram", "voice"…
  function channelOf(method) {
    const m = method ?? "";
    if (m.startsWith("card:")) return sourceOf(m.slice(5));
    return sourceOf(m);
  }
  const ICONS = {
    page: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    agent: '<circle cx="12" cy="3.5" r="1.3"/><path d="M12 5v2"/><rect x="4.5" y="7" width="15" height="11" rx="4.5"/><circle cx="9.5" cy="12" r="1.2" fill="currentColor" stroke="none"/><circle cx="14.5" cy="12" r="1.2" fill="currentColor" stroke="none"/><path d="M9.5 14.8c1.4 1.1 3.6 1.1 5 0"/>',
    chatgpt: '<path d="M12 3l2.2 1.3 2.5-.3 1.3 2.2 2.3 1-.2 2.5 1.4 2.3-1.4 2.3.2 2.5-2.3 1-1.3 2.2-2.5-.3L12 21l-2.2-1.3-2.5.3-1.3-2.2-2.3-1 .2-2.5L2.5 12l1.4-2.3-.2-2.5 2.3-1 1.3-2.2 2.5.3z"/><circle cx="12" cy="12" r="3.2"/>',
    claude: '<path d="M12 3v18M4.2 7.5l15.6 9M4.2 16.5l15.6-9"/>',
    code: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M12 15h5"/>',
    telegram: '<path d="M21 4L3 11l6 2.5L11.5 20l3-4 4.5 3z"/><path d="M9 13.5L21 4"/>',
    voice: '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 005 5L15 13l5 2v4a2 2 0 01-2 2A16 16 0 013 6a2 2 0 012-2z"/>',
    mcp: '<circle cx="12" cy="12" r="3"/><path d="M12 3v4M12 17v4M3 12h4M17 12h4"/>',
    bought: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/>',
    declined: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    stopped: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.5"/>',
    chat: '<path d="M21 12a8 8 0 01-11.6 7.1L4 20l1-5A8 8 0 1121 12z"/>',
  };
  // Our own line icons, or a brand's mark (filled, in its colour).
  const icon = (name, cls = "") => {
    const span = el("span", { class: `src-icon ${cls}`, "aria-hidden": "true" });
    const brand = window.BRANDS?.[name];
    if (brand) {
      span.classList.add("mark", `mark-${name}`);
      span.innerHTML = `<svg viewBox="0 0 24 24"><path d="${brand.path}"/></svg>`;
    } else {
      span.innerHTML = `<svg viewBox="0 0 24 24">${ICONS[name] ?? ICONS.mcp}</svg>`;
    }
    return span;
  };
  // A source's icon: its brand mark, with a small extra mark for Claude Code.
  const sourceIcon = (key) => {
    const src = SOURCES[key] ?? SOURCES.other;
    const node = icon(src.icon);
    if (src.mark) node.append(Object.assign(icon(src.mark, "sub"), {}));
    return node;
  };
  const sourceBadge = (key, suffix = "") => el("span", { class: `src-badge src-${key}` }, sourceIcon(key), el("span", { text: (SOURCES[key]?.label ?? key) + suffix }));
  const channelBadge = (method) => sourceBadge(channelOf(method), (method ?? "").startsWith("card:") ? " · card" : "");

  const dayOf = (iso) => new Date(iso).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  const timeOf = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  const whenFull = (iso) => new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  // The list's entries: conversations, and decisions (an "approved" that
  // ended in a purchase or a stop is folded into that outcome).
  function historyItems(data) {
    const items = [];
    for (const c of data.conversations) {
      if (!c.messages.length && !c.proposals.length && !c.external.length) continue;
      const named = c.external.map((e) => (e.text.match(/^(.+?) \(over MCP\)/) || [])[1]).find(Boolean);
      const via = c.proposals.find((p) => p.via)?.via ?? named ?? (c.external.length ? "assistant" : null);
      items.push({ key: `c:${c.id}`, kind: "conversation", at: c.started_at, source: via ? sourceOf(via) : "page", conversation: c });
    }
    const followed = new Set(data.decisions.filter((d) => d.event === "purchased" || d.event === "stopped").map((d) => d.proposal_id));
    for (const d of data.decisions) {
      if (d.event === "approved" && followed.has(d.proposal_id)) continue;
      items.push({ key: `d:${d.at}:${d.event}:${d.proposal_id}`, kind: d.event === "purchased" ? "purchase" : "decision", at: d.at, source: channelOf(d.method), decision: d, proposal: data.proposals?.[d.proposal_id] });
    }
    items.sort((a, b) => (b.at ?? "").localeCompare(a.at ?? ""));
    return items;
  }

  function rowTitle(item) {
    if (item.kind === "conversation") {
      const c = item.conversation;
      const who = SOURCES[item.source]?.label ?? "You";
      return `${item.source === "page" ? "You" : who} and your agent${c.current ? " · this chat" : ""}`;
    }
    const d = item.decision;
    return ({ purchased: "Bought", declined: "Declined", stopped: "Stopped by the shop", approved: "Approved, paying" })[d.event] + `: ${d.item}`;
  }
  function rowMeta(item) {
    if (item.kind === "conversation") {
      const c = item.conversation;
      return [`${c.messages.length} message${c.messages.length === 1 ? "" : "s"}`, c.calls ? `${c.calls} calls to the shops` : null, c.proposals.length ? `${c.proposals.length} proposal${c.proposals.length === 1 ? "" : "s"}` : null, c.receipts.length ? `${c.receipts.length} bought` : null].filter(Boolean).join(" · ");
    }
    const d = item.decision;
    return [money(d.total) + (d.coins ? ` + ${d.coins} coins` : ""), item.proposal?.shop_name ?? null, d.why ? `shop said ${d.why.replaceAll("_", " ").toLowerCase()}` : null].filter(Boolean).join(" · ");
  }

  function drawHistory() {
    const root = $("#history");
    if (!historyData) return;
    let items = historyItems(historyData);
    if (historyKind === "purchases") items = items.filter((i) => i.kind === "purchase");
    if (historyKind === "decisions") items = items.filter((i) => i.kind !== "conversation");
    if (historyKind === "conversations") items = items.filter((i) => i.kind === "conversation");
    if (historySource !== "all") items = items.filter((i) => i.source === historySource);
    $("#history-summary").textContent = `${historyData.conversations.length} conversations · ${historyData.decisions.filter((d) => d.event === "purchased").length} purchases`;
    root.replaceChildren();
    if (!items.length) { root.append(el("p", { class: "empty-note", text: "Nothing here yet." })); return; }
    let day = null;
    for (const item of items) {
      const d = item.at ? dayOf(item.at) : "";
      if (d !== day) { day = d; root.append(el("h3", { class: "h-day", text: d })); }
      const kindIcon = item.kind === "conversation" ? "chat" : item.decision.event === "purchased" ? "bought" : item.decision.event === "declined" ? "declined" : "stopped";
      root.append(el("button", { type: "button", class: `h-row ${item.kind === "conversation" ? "" : item.decision.event} ${historyOpen === item.key ? "on" : ""}`, "data-key": item.key, onclick: () => openHistory(item) },
        el("span", { class: "h-src", title: SOURCES[item.source]?.label }, sourceIcon(item.source)),
        el("span", { class: "h-what" },
          el("b", {}, icon(kindIcon, "kind"), el("span", { text: rowTitle(item) })),
          el("small", { text: rowMeta(item) })),
        el("time", { text: timeOf(item.at) })));
    }
  }

  function drawSourceChips() {
    const present = [...new Set(historyItems(historyData).map((i) => i.source))];
    const order = ["page", "agent", "chatgpt", "codex", "claude", "desktop", "code", "telegram", "voice", "other"];
    const chips = $("#source-chips");
    chips.replaceChildren(
      el("button", { type: "button", class: `chip ${historySource === "all" ? "on" : ""}`, "data-source": "all", text: "Any source" }),
      ...order.filter((k) => present.includes(k)).map((k) => el("button", { type: "button", class: `chip with-icon ${historySource === k ? "on" : ""}`, "data-source": k }, sourceIcon(k), el("span", { text: SOURCES[k].label }))));
    if (historySource !== "all" && !present.includes(historySource)) historySource = "all";
  }

  async function loadHistory() {
    const root = $("#history");
    if (!historyData) root.replaceChildren(el("p", { class: "muted", text: "Loading…" }));
    try { historyData = await (await fetch("/api/history")).json(); } catch { root.replaceChildren(el("p", { class: "muted", text: "Couldn't read the journal." })); return; }
    drawSourceChips();
    drawHistory();
  }

  // ---- The side panel

  function facts(rows) {
    return el("dl", { class: "h-facts" }, rows.filter(([, v]) => v != null && v !== "").map(([k, v]) => [el("dt", { text: k }), el("dd", {}, v)]));
  }

  function conversationPanel(item) {
    const c = item.conversation;
    const transcript = el("div", { class: "h-transcript" }, (c.items ?? []).map((i) => {
      if (i.kind === "external") return el("div", { class: "t-ext" }, sourceIcon(item.source), el("span", { text: i.text }));
      if (i.kind === "message" && i.role === "person") return el("div", { class: "t-person" }, el("span", { text: i.text }));
      if (i.kind === "message") return el("div", { class: "t-agent" }, icon("agent"), el("span", { text: i.text }));
      if (i.kind === "proposal") return el("div", { class: "t-card" },
        i.image && el("img", { src: i.image, alt: "" }),
        el("div", {}, el("b", { text: i.title }), el("small", { text: `${i.shop_name} · card pays ${money(i.total)}${i.via ? " · proposed by " + i.via : ""}` })));
      if (i.kind === "receipt") return el("div", { class: "t-card good" },
        el("div", {}, el("b", { text: `Bought: ${i.title}` }), el("small", {}, `${i.shop_name} · `, (i.codes ?? []).map((code) => el("code", { text: code })), " · ", el("a", { href: `/receipts/${i.order_id}`, text: "receipt" }), " · ", el("a", { href: `/evidence/${i.order_id}`, text: "evidence" }))));
      return null;
    }));
    return [
      el("header", { class: "h-head" },
        el("div", {},
          el("p", { class: "eyebrow" }, sourceBadge(item.source), el("span", { text: ` · ${whenFull(c.started_at)}` })),
          el("h3", { text: rowTitle(item) }),
          el("p", { class: "muted", text: rowMeta(item) })),
        el("button", { class: "close", type: "button", "aria-label": "Close", text: "×", onclick: closeHistory })),
      transcript,
    ];
  }

  function decisionPanel(item) {
    const d = item.decision;
    const p = item.proposal;
    const outcome = { purchased: ["Bought, at the total you approved.", "good"], declined: ["You turned this down. Nothing was paid.", "bad"], stopped: ["Approved by you, but the shop stopped it. Nothing was paid.", "bad"], approved: ["Approved; paying the shop.", ""] }[d.event] ?? [d.event, ""];
    const chain = (historyData.decisions ?? []).filter((x) => x.proposal_id === d.proposal_id).sort((a, b) => a.at.localeCompare(b.at));
    return [
      el("header", { class: "h-head" },
        el("div", {},
          el("p", { class: "eyebrow" }, channelBadge(d.method), el("span", { text: ` · ${whenFull(d.at)}` })),
          el("h3", { text: d.item }),
          el("p", { class: `h-outcome ${outcome[1]}` }, icon(d.event === "purchased" ? "bought" : d.event === "declined" ? "declined" : "stopped"), el("span", { text: outcome[0] }))),
        el("button", { class: "close", type: "button", "aria-label": "Close", text: "×", onclick: closeHistory })),
      p?.image && el("img", { class: "h-photo", src: p.image, alt: "" }),
      facts([
        ["Shop", p?.shop_name],
        ["Card paid", money(d.total)],
        ["Coins", d.coins ? `${d.coins} from your wallet` : null],
        ["Proposed by", p?.via ? sourceBadge(sourceOf(p.via)) : sourceBadge("agent")],
        ["Decided", channelBadge(d.method)],
        ["Why the agent chose it", p?.reason],
        ["Shop's answer", d.why ? d.why.replaceAll("_", " ").toLowerCase() : null],
        ["Order", d.order_id ? el("code", { text: d.order_id }) : null],
        ["Proposal", el("code", { text: d.proposal_id })],
      ]),
      d.order_id && el("div", { class: "h-actions" },
        el("a", { class: "secondary", href: `/receipts/${d.order_id}`, text: "Receipt" }),
        el("a", { class: "secondary", href: `/evidence/${d.order_id}`, text: "Evidence" })),
      el("h4", { text: "What happened" }),
      el("ol", { class: "h-timeline" }, chain.map((x) => el("li", {},
        el("time", { text: whenFull(x.at) }),
        el("span", { text: ({ approved: "Approved", purchased: "Bought", declined: "Declined", stopped: "Stopped by the shop" })[x.event] ?? x.event }),
        el("small", {}, " · ", channelBadge(x.method), x.why ? ` · ${x.why.replaceAll("_", " ").toLowerCase()}` : "")))),
    ];
  }

  // A list with a side panel: the panel slides in, the list yields.
  function openPanel(split, panel, nodes) {
    panel.hidden = false;
    requestAnimationFrame(() => requestAnimationFrame(() => split.classList.add("open")));
    const body = el("div", { class: "h-panel-body" }, ...nodes.flat().filter(Boolean));
    panel.replaceChildren(body);
    panel.scrollTop = 0;
    requestAnimationFrame(() => body.classList.add("in"));
  }
  function closePanel(split, panel) {
    split.classList.remove("open");
    const done = () => { if (!split.classList.contains("open")) panel.hidden = true; panel.removeEventListener("transitionend", done); };
    panel.addEventListener("transitionend", done);
    setTimeout(done, 450);
  }

  function openHistory(item) {
    historyOpen = item.key;
    drawHistory();
    openPanel($("#history-split"), $("#history-panel"), item.kind === "conversation" ? conversationPanel(item) : decisionPanel(item));
  }

  function closeHistory() {
    historyOpen = null;
    closePanel($("#history-split"), $("#history-panel"));
    drawHistory();
  }

  // ---- Approvals: what waits for a yes, from any channel

  let focusProposal = null;  // from /approvals/<id>: the one to show first

  function approvalsBadge(count) {
    const badge = $("#approvals-badge");
    badge.textContent = count;
    badge.hidden = !count;
  }

  async function refreshApprovals() {
    let data;
    try { data = await (await fetch("/api/approvals")).json(); } catch { return; }
    approvalsBadge(data.pending.length);
    if ($("[data-view=approvals]").hidden) return;
    const list = $("#approvals");
    list.replaceChildren();
    $("#approvals-summary").textContent = data.pending.length ? `${data.pending.length} waiting` : "Nothing waiting";
    if (!data.pending.length) {
      list.append(el("p", { class: "empty-note", text: "Nothing waits for you right now. Ask your agent for something, or let your other assistant propose through MCP." }));
    }
    for (const proposal of data.pending) {
      const card = proposalCard(proposal, list);
      if (proposal.id === focusProposal) {
        card.classList.add("focus");
        requestAnimationFrame(() => card.scrollIntoView({ block: "start", behavior: "smooth" }));
        focusProposal = null;
      }
    }
    $("#decided-title").hidden = !data.decided.length;
    $("#decided").replaceChildren(...data.decided.map((p) => el("li", { class: p.status },
      el("span", { class: "state-dot" }),
      el("span", { class: "what" }, el("b", { text: `${p.kind === "cancel" ? "Cancel " : p.kind === "reschedule" ? "Move " : ""}${p.title}` }), el("small", {}, `${p.shop_name} · ${p.kind === "cancel" ? "refund" : p.kind === "reschedule" ? "new time" : money(p.total)} · proposed by `, sourceBadge(p.via ? sourceOf(p.via) : "agent"), p.decided_via ? [" · decided on ", channelBadge(p.decided_via)] : null)),
      el("span", { class: "verdict", text: SHORT[p.status] ?? p.status }))));
  }

  // ---- A receipt: what was bought, the voucher and what was charged

  function receiptCard(receipt) {
    const paid = [];
    if (receipt.coins) paid.push({ type: "coins", label: `Paid with ${coinsText(receipt.coins)}`, amount: -receipt.coins * 100 });
    paid.push({ type: "total", label: `Charged to your card${receipt.rail === "mock" ? " (simulated)" : ""}`, amount: receipt.charged ?? 0 });
    const exact = receipt.approved == null || receipt.charged === receipt.approved;
    add(el("article", { class: "card receipt" },
      el("div", { class: "head plain" },
        el("div", {},
          el("span", { class: "tag", text: "Bought · your voucher" }),
          el("h3", { text: receipt.title }),
          el("p", { class: "seller" }, dot(receipt.shop), ` ${receipt.shop_name} · order ${receipt.order_id.slice(0, 8)}`))),
      el("ul", { class: "codes", "aria-label": "Voucher codes" }, receipt.codes.map((code) => el("li", { text: code }))),
      receipt.service ? terms(receipt) : null,
      el("div", { class: "pay" },
        sum(paid),
        el("p", { class: exact ? "match" : "match off",
          text: receipt.approved == null ? `Charged ${money(receipt.charged ?? 0)}.`
            : exact ? `Charged exactly what you approved: ${money(receipt.approved)}.`
            : `You approved ${money(receipt.approved)} and the shop charged ${money(receipt.charged ?? 0)}.` }),
        receipt.coins_earned > 0 && el("p", { class: "earn", text: `You earned ${coinsText(receipt.coins_earned)} at ${receipt.shop_name}.` }),
        el("p", { class: "pledge" }, "It's also in your account at the shop: ",
          el("a", { href: receipt.url ?? `${shops.get(receipt.shop)?.url ?? ""}/vouchers/${receipt.order_id}`, target: "_blank", rel: "noopener", text: `open it in ${receipt.shop_name}` }), ". ",
          "If anyone asks who bought this: ",
          el("a", { href: receipt.evidence_url ?? `/evidence/${receipt.order_id}`, text: "the evidence" }), ". ",
          el("a", { href: `/receipts/${receipt.order_id}`, text: "Receipt" }), "."))));
  }

  // ---- The sidebar: the shops and the coins held in each

  function drawShops() {
    $("#shops").replaceChildren(...[...shops.values()].map((shop) =>
      el("li", { "data-shop": shop.id },
        dot(shop.id),
        el("span", {},
          el("a", { href: shop.url, target: "_blank", rel: "noopener", text: shop.name }),
          el("small", { "data-back": true, text: "Coins not read yet" })),
        el("span", { class: "coins none", "data-coins": true, text: "–" }))));
  }

  function wallets(list) {
    for (const wallet of list) {
      const row = $(`[data-shop="${wallet.shop}"]`);
      if (!row) continue;
      const coins = $("[data-coins]", row);
      const text = wallet.balance ? coinsText(wallet.balance) : "No coins";
      if (coins.textContent !== "–" && coins.textContent !== text) {
        coins.classList.remove("moved");
        void coins.offsetWidth;
        coins.classList.add("moved");
      }
      coins.textContent = text;
      coins.classList.toggle("none", !wallet.balance);
      $("[data-back]", row).textContent = wallet.back_percent
        ? `Gives ${wallet.back_percent}% back in coins` : "Gives no coins";
    }
  }

  // ---- What the agent did: its requests to the shops, turn by turn

  function activity(call) {
    const shop = shops.get(call.shop);
    if (!steps) {
      steps = add(el("details", { class: "steps" }, el("summary", {}, el("span")), el("ol")));
      steps.calls = [];
    }
    steps.calls.push(call);
    const refused = call.status >= 400 || call.status === 0;
    $("ol", steps).append(el("li", {},
      dot(call.shop),
      el("code", { text: `${call.method} ${call.path.replace(/[0-9a-f]{8}-[0-9a-f-]{27}/, (id) => id.slice(0, 8) + "…")}` }),
      el("span", { class: refused ? "status refused" : "status", text: call.status || "down" }),
      el("small", { text: `${shop?.name ?? call.shop} · ${call.note} · signed · ${call.ms} ms` })));
    steps.classList.toggle("live", busy);
    $("summary span", steps).textContent = busy ? `${shop?.name ?? "A shop"}: ${call.note}` : stepsSummary(steps.calls);
  }

  function stepsSummary(calls) {
    const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
    const refused = calls.filter((call) => call.status >= 400 || call.status === 0).length;
    return `${count(calls.length, "signed request")} to ${count(new Set(calls.map((call) => call.shop)).size, "shop")}`
      + (refused ? ` · ${refused} refused` : "");
  }

  // A turn's steps are complete: sum them up, and start afresh next time.
  function closeSteps() {
    if (!steps) return;
    steps.classList.remove("live");
    $("summary span", steps).textContent = stepsSummary(steps.calls);
    steps = null;
  }

  // ---- Events

  // A live event: drawn once, in order.
  function apply(event) {
    if (event.seq <= lastSeq) return;
    lastSeq = event.seq;
    draw(event);
  }

  // One entry of the thread, live or from the journal: the same drawing.
  function draw(item) {
    // Anything said or shown closes the run of calls to the shops before it.
    if (item.type !== "activity" && item.type !== "wallets" && item.type !== "memory") closeSteps();
    if (["person", "agent", "proposal", "receipt", "external"].includes(item.type)) daySep(item.at);
    switch (item.type) {
      case "person": message("person", item.text, item.via); break;
      case "agent": message("agent", item.text, item.via); break;
      case "activity": activity(item); break;
      case "wallets": wallets(item.wallets); break;
      case "proposal": item.brief ? briefProposal(item.proposal, item.via) : proposalCard(item.proposal); break;
      case "proposal_status": proposalStatus(item.proposal_id ?? item.id, item.status, item.method); break;
      case "receipt": receiptCard(item.receipt); break;
      case "memory": if (!$("[data-view=memory]").hidden) showMemory(item.memory); break;
      case "external": add(el("p", { class: "external" }, chan(item.via, "agent"), el("span", { text: item.text }))); break;
    }
  }

  // An old proposal the agent no longer holds whole: what the journal kept.
  function briefProposal(p, via) {
    add(el("article", { class: "card proposal brief closed", "data-proposal": p.id },
      el("div", { class: "head plain" },
        el("div", {},
          el("span", { class: "tag", "data-tag": true, text: "Proposal" }),
          p.via && el("span", { class: "via", text: `Proposed by ${p.via}` }),
          el("h3", { text: p.title }),
          el("p", { class: "seller" }, dot(p.shop), ` ${p.shop_name} · ${money(p.total ?? 0)}`))),
      p.image && el("img", { class: "h-photo", src: p.image, alt: "" }),
      el("div", { class: "pay" })));
  }

  // ---- The thread: the journal, a page at a time, newest at the bottom

  let oldestId = null;   // the first journal line drawn; paging goes before it
  let hasEarlier = false;
  const earlier = $("#earlier");

  function drawPage(items, { prepend = false } = {}) {
    const fragment = document.createDocumentFragment();
    target = fragment;
    const dayBefore = lastDay;
    if (prepend) lastDay = null;
    const liveSteps = steps; steps = null;
    for (const item of items) draw(item);
    closeSteps();
    steps = liveSteps;
    target = null;
    if (prepend) {
      // Keep what the person was looking at where it is.
      const height = feed.scrollHeight;
      earlier.after(fragment);
      // The day the page ended on may already head the old first entry.
      const seps = feed.querySelectorAll(".day-sep");
      for (let i = 1; i < seps.length; i++) if (seps[i].textContent === seps[i - 1].textContent) seps[i].remove();
      feed.scrollTop += feed.scrollHeight - height;
      lastDay = dayBefore ?? lastDay;
    } else {
      feed.insertBefore(fragment, working);
    }
    if (items.length) begin();
  }

  async function loadThread() {
    let page;
    try { page = await (await fetch("/api/thread?limit=60")).json(); }
    catch { add(el("p", { class: "external", text: "Couldn't read the thread. What happens from here on still shows." })); return; }
    feed.classList.add("bulk");
    drawPage(page.items);
    feed.classList.remove("bulk");
    oldestId = page.items[0]?.id ?? null;
    hasEarlier = page.has_more;
    earlier.hidden = !hasEarlier;
    // The live events continue from here; the journal had the rest.
    lastSeq = page.seq_now - 1;
    toNewest();
    // Photos arriving in the next moments push the thread down: follow.
    const settle = Date.now() + 2500;
    feed.addEventListener("load", (e) => { if (e.target.tagName === "IMG" && Date.now() < settle) toNewest(); }, true);
    setTimeout(toNewest, 400);
    refreshApprovals();
  }

  // Straight to the newest, without the smooth scroll of the live feed.
  function toNewest() {
    feed.style.scrollBehavior = "auto";
    feed.scrollTop = feed.scrollHeight;
    if (!feedScrolls()) window.scrollTo(0, document.documentElement.scrollHeight);
    requestAnimationFrame(() => { feed.style.scrollBehavior = ""; });
  }

  async function rebuildThread() {
    closeSteps();
    for (const node of [...feed.children]) if (node !== earlier && node !== working) node.remove();
    lastDay = null;
    lastSeq = -1;
    await loadThread();
  }

  let loadingEarlier = false;
  async function loadEarlier() {
    if (!hasEarlier || loadingEarlier || oldestId == null) return;
    loadingEarlier = true;
    earlier.disabled = true;
    try {
      const page = await (await fetch(`/api/thread?before=${oldestId}&limit=60`)).json();
      feed.classList.add("bulk");
      drawPage(page.items, { prepend: true });
      feed.classList.remove("bulk");
      if (page.items.length) oldestId = page.items[0].id;
      hasEarlier = page.has_more;
      earlier.hidden = !hasEarlier;
    } finally { loadingEarlier = false; earlier.disabled = false; }
  }
  earlier.addEventListener("click", loadEarlier);
  // Scrolling up to the top asks for more by itself; nothing loads unasked.
  // The feed scrolls on a desk; on a phone it is the page that scrolls.
  const feedScrolls = () => getComputedStyle(feed).overflowY !== "visible";
  const nearTop = () => (feedScrolls() ? feed.scrollTop : window.scrollY) < 120;
  feed.addEventListener("scroll", () => { if (nearTop()) loadEarlier(); }, { passive: true });
  window.addEventListener("scroll", () => { if (!talk.hidden && nearTop()) loadEarlier(); }, { passive: true });

  function setBusy(on) {
    busy = on;
    for (const button of document.querySelectorAll("#composer button, [data-approve], [data-decline], [data-idea], .pick")) {
      button.disabled = on;
    }
    if (on && !working) {
      working = el("div", { class: "working", role: "status", "aria-label": "The agent is working" });
      feed.append(working);
      working.scrollIntoView({ block: "end", behavior: "smooth" });
    } else if (!on && working) {
      working.remove();
      working = null;
    }
    if (!on) closeSteps();
    $("#composer button").disabled = on || !$("#say").value.trim();
  }

  // Send something to the agent and draw its events as they arrive.
  async function run(url, body = {}) {
    if (busy) return;
    begin();
    setBusy(true);
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        pending += decoder.decode(value, { stream: true });
        const lines = pending.split("\n");
        pending = lines.pop();
        for (const line of lines) if (line.trim()) apply(JSON.parse(line));
      }
    } catch (error) {
      console.error(error);
      message("agent", "I lost the connection to my server. Reload the page to see where things stand.");
    } finally {
      setBusy(false);
    }
  }

  // ---- The composer

  const composer = $("#composer");
  const box = $("#say");

  function send(text) {
    text = text.trim();
    if (!text || busy) return;
    box.value = "";
    box.style.height = "";
    run("/api/messages", { text });
  }

  composer.addEventListener("submit", (event) => {
    event.preventDefault();
    send(box.value);
  });
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send(box.value);
    }
  });
  // The send button is live only when there is something to send.
  const sendButton = $("button", composer);
  const readySend = () => { sendButton.disabled = busy || !box.value.trim(); };
  box.addEventListener("input", () => {
    box.style.height = "";
    box.style.height = `${box.scrollHeight}px`;
    readySend();
  });
  document.addEventListener("click", (event) => {
    const idea = event.target.closest("[data-idea]");
    if (idea) send(idea.textContent);
  });

  // ---- Views: chat, your purchases, what the agent knows

  // ---- Your purchases: the vouchers, as the shops see them today

  // A purchase's state, from the shop's view of the voucher: one word for
  // the row, a sentence for the sheet, and what to do next.
  const VSTATE = {
    ready:     { label: "Ready to use",   kind: "",     icon: "ticket",   says: "Ready to use: show the code at the venue." },
    used:      { label: "Used",           kind: "good", icon: "bought",   says: "Used at the venue. All done." },
    refunded:  { label: "Refunded",       kind: "bad",  icon: "refund",   says: "Refunded: the card and the coins went back. The code is void." },
    cancelled: { label: "Cancelled",      kind: "bad",  icon: "declined", says: "The venue cancelled; the shop owes you a refund." },
    failed:    { label: "Turned away",    kind: "bad",  icon: "stopped",  says: "You were turned away at the venue. Ask the shop for a refund." },
    expired:   { label: "Expired",        kind: "bad",  icon: "stopped",  says: "The promotional value expired. What you paid never does: ask the shop." },
    gone:      { label: "Not at the shop", kind: "",    icon: "stopped",  says: "The shop doesn't know this order any more." },
  };
  Object.assign(ICONS, {
    ticket: '<path d="M4 8a2 2 0 012-2h12a2 2 0 012 2v2a2 2 0 000 4v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2a2 2 0 000-4z"/><path d="M10 6v12" stroke-dasharray="2 2"/>',
    refund: '<path d="M4 10a8 8 0 0114-4.9M20 14a8 8 0 01-14 4.9"/><path d="M18 2v4h-4M6 22v-4h4"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 011-1h10"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    pin: '<path d="M12 21s-6-5.3-6-10a6 6 0 0112 0c0 4.7-6 10-6 10z"/><circle cx="12" cy="11" r="2"/>',
  });
  function stateOf(p) {
    if (p.error) return "gone";
    if (p.payment === "refunded" || p.voucher === "refunded") return "refunded";
    if (p.redemption === "redeemed") return "used";
    if (p.redemption === "cancelled_by_merchant") return "cancelled";
    if (p.redemption === "redemption_failed") return "failed";
    const expires = p.voucher_terms?.expires_at;
    if (expires && new Date(expires) < new Date()) return "expired";
    return "ready";
  }
  const pill = (text, kind) => el("span", { class: `pill ${kind}`, text });
  const statePill = (key) => pill(VSTATE[key].label, VSTATE[key].kind);

  // "in 3 days", "tomorrow", "4 days ago": for expiry and service windows.
  function relative(iso) {
    const days = Math.round((new Date(iso) - Date.now()) / 86400000);
    if (days === 0) return "today";
    if (days === 1) return "tomorrow";
    if (days === -1) return "yesterday";
    return days > 0 ? `in ${days} days` : `${-days} days ago`;
  }
  const dayShort = (iso) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  function windowText(w) {
    if (!w?.not_before) return null;
    const a = new Date(w.not_before), b = w.not_after ? new Date(w.not_after) : null;
    const sameDay = b && a.toDateString() === b.toDateString();
    return `${whenFull(w.not_before)}${b ? (sameDay ? ` to ${timeOf(w.not_after)}` : ` to ${whenFull(w.not_after)}`) : ""}`;
  }
  // The one line the row needs under the title: what matters next.
  function nextStep(p, state) {
    const b = p.service?.booking;
    if (state === "ready" && b?.starts_at) return `Booked for ${dayShort(b.starts_at)} ${timeOf(b.starts_at)}, ${relative(b.starts_at)}`;
    const w = p.service?.window;
    if (state === "ready" && w?.not_before) return `${new Date(w.not_before) > Date.now() ? "Happens" : "Started"} ${relative(w.not_before)}, ${dayShort(w.not_before)}`;
    if (state === "ready" && p.redemption_terms?.appointment_required) return "Book first" + (p.redemption_terms.booking_contact ? `: ${p.redemption_terms.booking_contact}` : "");
    if (state === "ready" && p.voucher_terms?.expires_at) return `Valid until ${dayShort(p.voucher_terms.expires_at)} (${relative(p.voucher_terms.expires_at)})`;
    if (state === "ready") return "Any day, no expiry";
    return null;
  }
  const REDEMPTION = { code_at_venue: "Show the code at the venue", code_online: "Enter the code when you book online", qr_at_door: "Show the QR at the door" };
  function redemptionText(red) {
    if (!red?.method) return null;
    const how = REDEMPTION[red.method] ?? red.method.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
    return how + (red.appointment_required ? " · appointment required" : "");
  }
  function paidText(p) {
    const card = p.charged ?? p.approved ?? 0;
    const parts = [card || !p.coins ? `${money(card)} card` : null, p.coins ? `${coinsText(p.coins)}` : null].filter(Boolean);
    return parts.join(" + ");
  }

  let purchaseRows = [];
  let purchaseState = "all";
  let purchaseShop = "all";
  let purchaseOpen = null;   // order_id in the panel

  function purchaseKpis(rows) {
    const live = rows.filter((p) => stateOf(p) !== "refunded");
    const spent = live.reduce((n, p) => n + (p.charged ?? p.approved ?? 0), 0);
    const coinsUsed = live.reduce((n, p) => n + (p.coins ?? 0), 0);
    const earned = live.reduce((n, p) => n + (p.coins_earned ?? 0), 0);
    const ready = rows.filter((p) => stateOf(p) === "ready").length;
    const used = rows.filter((p) => stateOf(p) === "used").length;
    const refunded = rows.filter((p) => stateOf(p) === "refunded").length;
    const shopsIn = new Set(rows.map((p) => p.shop)).size;
    const kpi = (big, small, extra) => el("div", { class: "kpi" }, el("b", { text: big }), el("span", { text: small }), extra ? el("small", { text: extra }) : null);
    return [
      kpi(money(spent), "spent on the card", refunded ? `${refunded} refunded, not counted` : null),
      kpi(String(ready), ready === 1 ? "voucher ready to use" : "vouchers ready to use", used ? `${used} already used` : null),
      kpi(String(earned), "coins earned", coinsUsed ? `${coinsUsed} spent` : "spend them at the same shop"),
      kpi(String(rows.length), rows.length === 1 ? "purchase" : "purchases", `across ${shopsIn} shop${shopsIn === 1 ? "" : "s"}`),
    ];
  }

  function drawPurchaseChips() {
    const present = new Set(purchaseRows.map(stateOf));
    const order = ["ready", "used", "refunded", "cancelled", "failed", "expired", "gone"];
    if (purchaseState !== "all" && !present.has(purchaseState)) purchaseState = "all";
    $("#purchase-state-chips").replaceChildren(
      el("button", { type: "button", class: `chip ${purchaseState === "all" ? "on" : ""}`, "data-state": "all", text: "Everything" }),
      ...order.filter((k) => present.has(k)).map((k) => el("button", { type: "button", class: `chip with-icon ${purchaseState === k ? "on" : ""}`, "data-state": k }, icon(VSTATE[k].icon), el("span", { text: VSTATE[k].label }))));
    const shopsSeen = [...new Map(purchaseRows.map((p) => [p.shop, p.shop_name])).entries()];
    if (purchaseShop !== "all" && !shopsSeen.some(([id]) => id === purchaseShop)) purchaseShop = "all";
    $("#purchase-shop-chips").replaceChildren(
      ...(shopsSeen.length > 1 ? [el("button", { type: "button", class: `chip ${purchaseShop === "all" ? "on" : ""}`, "data-shop": "all", text: "Any shop" })] : []),
      ...(shopsSeen.length > 1 ? shopsSeen.map(([id, name]) => el("button", { type: "button", class: `chip with-icon ${purchaseShop === id ? "on" : ""}`, "data-shop": id }, dot(id), el("span", { text: name }))) : []));
  }

  function drawPurchases() {
    const root = $("#purchases");
    let rows = purchaseRows;
    if (purchaseState !== "all") rows = rows.filter((p) => stateOf(p) === purchaseState);
    if (purchaseShop !== "all") rows = rows.filter((p) => p.shop === purchaseShop);
    const kpis = $("#purchase-kpis");
    kpis.hidden = !purchaseRows.length;
    kpis.replaceChildren(...purchaseKpis(purchaseRows));
    const spent = purchaseRows.filter((p) => stateOf(p) !== "refunded").reduce((n, p) => n + (p.charged ?? p.approved ?? 0), 0);
    $("#purchases-summary").textContent = purchaseRows.length ? `${purchaseRows.length} purchase${purchaseRows.length === 1 ? "" : "s"} · ${money(spent)} on the card` : "";
    root.replaceChildren();
    if (!purchaseRows.length) { root.append(el("p", { class: "empty-note", text: "Nothing bought yet. Ask your agent for something." })); return; }
    if (!rows.length) { root.append(el("p", { class: "empty-note", text: "Nothing like that." })); return; }
    let day = null;
    for (const p of rows) {
      const d = dayOf(p.at);
      if (d !== day) { day = d; root.append(el("h3", { class: "h-day", text: d })); }
      const state = stateOf(p);
      const step = nextStep(p, state);
      root.append(el("button", { type: "button", class: `h-row p-row ${state} ${purchaseOpen === p.order_id ? "on" : ""}`, "data-order": p.order_id, onclick: () => openPurchase(p) },
        el("span", { class: "h-src shop" }, dot(p.shop)),
        el("span", { class: "h-what" },
          el("b", {}, icon(VSTATE[state].icon, "kind"), el("span", { text: p.title })),
          el("small", {}, el("span", { text: [p.shop_name, paidText(p)].filter(Boolean).join(" · ") }), step ? el("span", { class: "step", text: ` · ${step}` }) : null)),
        p.codes[0] && el("code", { class: "p-code", text: p.codes[0] + (p.codes.length > 1 ? ` +${p.codes.length - 1}` : "") }),
        el("span", { class: "p-end" }, statePill(state), el("time", { text: timeOf(p.at) }))));
    }
  }

  function copyButton(text) {
    return el("button", { type: "button", class: "copy", title: "Copy", "aria-label": "Copy the code", onclick: async (e) => {
      try { await navigator.clipboard.writeText(text); } catch { return; }
      const b = e.currentTarget; b.classList.add("done"); setTimeout(() => b.classList.remove("done"), 1200);
    } }, icon("copy"));
  }

  function purchasePanel(p) {
    const state = stateOf(p);
    const S = VSTATE[state];
    const decisions = (historyData?.decisions ?? []).filter((x) => x.proposal_id && x.proposal_id === p.proposal_id).sort((a, b) => a.at.localeCompare(b.at));
    const approved = decisions.find((x) => x.event === "approved");
    const terms = p.voucher_terms ?? {}, red = p.redemption_terms ?? {}, can = p.cancellation ?? {};
    const refundLine = can.refundability === "non_refundable" ? "Non-refundable"
      : can.refundable_until ? `Refundable until ${whenFull(can.refundable_until)} (${relative(can.refundable_until)})`
      : can.refundability ? can.refundability.replaceAll("_", " ") : null;
    const timeline = [
      approved && { at: approved.at, text: "Approved", extra: channelBadge(approved.method) },
      { at: p.at, text: "Bought and the voucher issued", extra: p.rail ? `${p.rail === "stripe" ? "Stripe" : "mock rail"}${p.payment_id ? " · " + p.payment_id : ""}` : null },
      p.service?.booking?.starts_at && { at: p.service.booking.starts_at, text: "Your visit", extra: bookedText(p.service.booking), future: new Date(p.service.booking.starts_at) > Date.now() },
      !p.service?.booking?.starts_at && p.service?.window?.not_before && { at: p.service.window.not_before, text: "The service", extra: windowText(p.service.window), future: new Date(p.service.window.not_before) > Date.now() },
      terms.expires_at && state === "ready" && { at: terms.expires_at, text: "Promotional value expires", future: new Date(terms.expires_at) > Date.now() },
      state === "used" && { text: "Used at the venue", extra: "recorded by the merchant" },
      state === "cancelled" && { text: "Cancelled by the venue" },
      state === "failed" && { text: "Turned away at the venue" },
      state === "refunded" && { text: "Refunded", extra: `${money(p.charged ?? 0)} to the card${p.coins ? ` · ${coinsText(p.coins)} to the wallet` : ""}` },
    ].filter(Boolean);
    const ticket = el("div", { class: `ticket ${state}` },
      el("div", { class: "t-top" },
        el("span", { class: "t-shop" }, dot(p.shop), el("span", { text: p.shop_name })),
        p.quantity > 1 && el("span", { class: "t-qty", text: `×${p.quantity}` })),
      p.codes.length
        ? el("div", { class: "t-codes" }, p.codes.map((c) => el("div", { class: "t-code" }, el("code", { text: c }), copyButton(c))))
        : state === "gone" ? null : el("p", { class: "muted", text: "No code on this order." }),
      el("p", { class: `t-says ${S.kind}` }, icon(S.icon), el("span", { text: state === "gone" && p.error ? `${S.says} It said: ${p.error}.` : S.says })),
      state === "ready" && red.instructions && el("p", { class: "t-how", text: red.instructions }),
      state === "ready" && red.booking_contact && el("p", { class: "t-how", text: `Book first: ${red.booking_contact}` }));
    return [
      el("header", { class: "h-head" },
        el("div", {},
          el("p", { class: "eyebrow" }, channelBadge(p.method), el("span", { text: ` · ${whenFull(p.at)}` })),
          el("h3", { text: p.title }),
          el("p", { class: "muted", text: [p.service?.option && p.service.option !== p.title ? p.service.option : null, p.service?.merchant, p.service?.location].filter(Boolean).join(" · ") })),
        el("button", { class: "close", type: "button", "aria-label": "Close", text: "×", onclick: closePurchase })),
      p.image && el("img", { class: "h-photo", src: p.image, alt: "" }),
      ticket,
      el("h4", { text: "Paid" }),
      facts([
        ["Card", money(p.charged ?? p.approved ?? 0) + (p.payment === "refunded" ? " · refunded" : p.payment === "captured" ? " · captured" : p.payment ? ` · ${p.payment}` : "")],
        ["Coins", p.coins ? `${coinsText(p.coins)} from your wallet` : null],
        ["Earned", p.coins_earned ? `${coinsText(p.coins_earned)} at ${p.shop_name}` : null],
        ["Rail", p.rail ? el("span", {}, p.rail === "stripe" ? "Stripe, test mode" : "Mock rail", p.payment_id ? el("code", { class: "soft", text: p.payment_id }) : null) : null],
        ["Refunds", refundLine],
      ]),
      el("h4", { text: "The deal" }),
      facts([
        ["When", windowText(p.service?.window) ?? (terms.expires_at ? `Any day, until ${whenFull(terms.expires_at)}` : "Any day")],
        ["Where", [p.service?.merchant, p.service?.location].filter(Boolean).join(", ") || null],
        ["Includes", p.service?.includes?.length ? p.service.includes.join(" · ") : null],
        ["How to use", redemptionText(red)],
        ["Proposed by", p.via ? sourceBadge(sourceOf(p.via)) : sourceBadge("agent")],
        ["Approved", channelBadge(p.method)],
        ["Why the agent chose it", p.reason],
        ["Order", el("code", { class: "soft", text: p.order_id })],
      ]),
      el("div", { class: "h-actions" },
        el("a", { class: "secondary", href: p.receipt_url, text: "Receipt" }),
        el("a", { class: "secondary", href: p.evidence_url, text: "Evidence" }),
        p.shop_url && el("a", { class: "secondary", href: p.shop_url, target: "_blank", rel: "noopener", text: `In ${p.shop_name} ↗` })),
      el("h4", { text: "What happened" }),
      el("ol", { class: "h-timeline" }, timeline.map((x) => el("li", { class: x.future ? "future" : "" },
        x.at ? el("time", { text: whenFull(x.at) + (x.future ? ` · ${relative(x.at)}` : "") }) : null,
        el("span", { text: x.text }),
        x.extra ? el("small", {}, " · ", x.extra) : null))),
    ];
  }

  function openPurchase(p) {
    purchaseOpen = p.order_id;
    drawPurchases();
    openPanel($("#purchases-split"), $("#purchases-panel"), purchasePanel(p));
  }
  function closePurchase() {
    purchaseOpen = null;
    closePanel($("#purchases-split"), $("#purchases-panel"));
    drawPurchases();
  }

  async function loadPurchases() {
    const root = $("#purchases");
    if (!purchaseRows.length) root.replaceChildren(el("p", { class: "muted", text: "Loading…" }));
    try {
      const [rows] = await Promise.all([
        (await fetch("/api/purchases")).json(),
        historyData ? null : fetch("/api/history").then((r) => r.json()).then((d) => { historyData = d; }).catch(() => null),
      ]);
      purchaseRows = rows;
    } catch { root.replaceChildren(el("p", { class: "muted", text: "Couldn't read your purchases." })); return; }
    drawPurchaseChips();
    drawPurchases();
    if (purchaseOpen) {
      const still = purchaseRows.find((p) => p.order_id === purchaseOpen);
      if (still) openPanel($("#purchases-split"), $("#purchases-panel"), purchasePanel(still)); else closePurchase();
    }
  }
  $("#purchase-state-chips").addEventListener("click", (e) => { const b = e.target.closest("[data-state]"); if (!b) return; purchaseState = b.dataset.state; drawPurchaseChips(); drawPurchases(); });
  $("#purchase-shop-chips").addEventListener("click", (e) => { const b = e.target.closest("[data-shop]"); if (!b) return; purchaseShop = b.dataset.shop; drawPurchaseChips(); drawPurchases(); });

  // ---- Settings: what the agent knows, in blocks that save themselves

  let memoryState = null;   // the memory as last loaded or saved
  let saveTimer = null;

  function savedNote(text) {
    const node = $("#settings-saved");
    node.textContent = text;
    if (text) setTimeout(() => { if (node.textContent === text) node.textContent = ""; }, 2500);
  }

  // Save the whole memory, a moment after the last change.
  function scheduleSave() {
    clearTimeout(saveTimer);
    savedNote("Saving…");
    saveTimer = setTimeout(saveMemory, 500);
  }
  async function saveMemory() {
    const m = memoryState;
    const body = { profile: m.profile, rules: m.rules, presentation: m.presentation };
    try {
      const response = await fetch("/api/memory", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!response.ok) throw new Error();
      memoryState = { ...memoryState, ...(await response.json()) };
      savedNote("Saved ✓");
    } catch { savedNote("Couldn't save"); }
  }

  // The page's own look (light, dark or the system's): kept in this browser.
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme === "system" ? "" : theme;
    try { localStorage.setItem("agent.theme", theme); } catch {}
  }
  try { applyTheme(localStorage.getItem("agent.theme") || "system"); } catch {}

  function seg(name, value) {
    for (const b of document.querySelectorAll(`.seg[data-setting="${name}"] button`)) b.classList.toggle("on", b.dataset.value === String(value));
  }

  // A sample proposal, laid out with the current presentation.
  function drawPreview() {
    const box = $("#look-preview");
    if (!box || !memoryState) return;
    const shop = [...shops.values()][0];
    const show = memoryState.presentation ?? {};
    const es = show.language === "es";
    const image = shop ? `${shop.url}/images/spa_day_two.jpg` : null;
    const deal = {
      title: es ? "Día de spa para dos" : "Spa Day for Two", gallery: image ? [image, image] : [], rating: { value: 4.7 },
      highlights: es ? ["Sauna y baño de vapor", "Piscina interior", "Toallas incluidas"] : ["Sauna and steam room", "Indoor pool", "Towels and slippers"],
      reviews: [{ rating: 5, text: es ? "Tranquilo y limpio. Volveremos." : "Quiet and clean. We'll be back.", author: "Ana" }],
      about_merchant: es ? "Un pequeño spa junto al río, en Praga 7." : "A small spa by the river, in Prague 7.",
    };
    const sample = {
      id: "preview", status: "pending", shop: shop?.id ?? "a", shop_name: shop?.name ?? "Dusk Deals",
      title: `${deal.title} · ${es ? "2 horas" : "2 hours"}`, quantity: 1, image,
      service: { merchant: "Lazne Holesovice", location: "Jankovcova 12, Prague 7", window: { not_before: "2026-10-10T10:00:00+02:00", not_after: "2026-10-10T20:00:00+02:00" } },
      cancellation: { refundability: "refundable", refundable_until: "2026-10-09T10:00:00+02:00" },
      voucher: { expires_at: "2026-12-31T23:59:00+01:00" }, redemption: { method: "code_at_venue", instructions: es ? "Enseña el código en recepción." : "Show the code at reception." },
      totals: [{ type: "subtotal", label: es ? "Precio" : "Price", amount: 6900 }, { type: "discount", label: es ? "Código DUSK10" : "Promo code DUSK10", amount: -690 }, { type: "coins", label: es ? "Pagado con 5 coins" : "Paid with 5 coins", amount: -500 }, { type: "total", label: es ? "Tu tarjeta paga" : "Your card pays", amount: 5710 }],
      total: 5710, currency: "USD", coins: { applied: 5, earns: 6 },
      reason: es ? "El spa reembolsable más barato para dos el día 10." : "The cheapest refundable spa for two on the 10th.",
      considered: [{ shop: "a", shop_name: shop?.name ?? "Dusk Deals", title: deal.title + " · 2 hours", price: 6900, verdict: "chosen", reason: es ? "El más barato con devolución" : "Cheapest, refundable" }, { shop: "c", shop_name: "Dawn Saver", title: "Spa Day for Two (Saver) · 2 hours", price: 4900, verdict: "rejected", reason: es ? "Sin devolución" : "Final sale" }],
      deal: { ...deal, option: { id: "spa_day_two_2h", label: es ? "2 horas" : "2 hours", price: 6900 }, refundability: "refundable", shop_name: shop?.name ?? "Dusk Deals" }, presentation: show,
      shortlist: show.shortlist === 3 ? [
        { ...deal, shop: "a", shop_name: shop?.name ?? "Dusk Deals", refundability: "refundable", option: { id: "spa_day_two_2h", label: es ? "2 horas" : "2 hours", price: 6900 } },
        { ...deal, shop: "a", shop_name: shop?.name ?? "Dusk Deals", refundability: "refundable", rating: { value: 4.6 }, title: es ? "Tarde de sauna privada para dos" : "Private Sauna Evening for Two", gallery: shop ? [`${shop.url}/images/sauna_evening_two.jpg`] : [], option: { id: "sauna_60", label: es ? "60 minutos" : "60 minutes", price: 5900 } },
        { ...deal, shop: "a", shop_name: shop?.name ?? "Dusk Deals", refundability: "refundable", rating: { value: 4.8 }, title: es ? "Sesión de flotación" : "Floatation Tank Session", gallery: shop ? [`${shop.url}/images/float_session.jpg`] : [], option: { id: "float_60", label: es ? "60 minutos" : "60 minutes", price: 5900 } },
      ] : [],
    };
    box.replaceChildren();
    try {
      const card = proposalCard(sample, box);
      card.classList.add("preview");
      for (const node of card.querySelectorAll("button")) { node.disabled = true; node.onclick = null; }
      const tag = card.querySelector("[data-tag]");
      if (tag) tag.textContent = es ? "Así verás una propuesta" : "How a proposal will look";
    } catch (error) { box.append(el("p", { class: "muted", text: "Preview unavailable." })); console.error(error); }
  }

  let capsInForce = null;  // the agent's own caps, enforced in its code
  function drawInForce() {
    const r = memoryState.rules;
    const chips = [];
    if (capsInForce) chips.push(`In code: ${money(capsInForce.per_purchase)} per purchase, ${money(capsInForce.per_day)} a day (${money(capsInForce.spent_today)} spent today)`);
    if (r.max_total != null) chips.push(`Never above ${money(r.max_total)}`);
    if (r.refundable_only) chips.push("Refundable only");
    for (const c of r.avoid_categories) chips.push(`No ${c}`);
    $("#in-force").replaceChildren(el("span", { class: "muted", text: chips.length ? "In force:" : "No rules yet: your agent may propose anything you ask for." }), ...chips.map((c) => el("span", { class: "pill good", text: c })));
  }

  async function drawUsedBy() {
    const box = $("#used-by");
    if (!box) return;
    try {
      const data = await (await fetch("/api/history")).json();
      const names = new Set();
      for (const c of data.conversations) {
        for (const p of c.proposals) if (p.via) names.add(sourceOf(p.via));
        for (const e of c.external) { const m = e.text.match(/^(.+?) \(over MCP\)/); if (m) names.add(sourceOf(m[1])); }
      }
      for (const d of data.decisions) if ((d.method ?? "").startsWith("card:")) names.add(channelOf(d.method));
      names.delete("page"); names.delete("agent");
      box.replaceChildren(...(names.size ? [...names].map((k) => sourceBadge(k)) : [el("span", { class: "muted", text: "None yet" })]));
    } catch { box.replaceChildren(el("span", { class: "muted", text: "–" })); }
  }

  // Telegram: linked, waiting for the person to open the link, or no bot.
  async function drawTelegram() {
    const note = $("#telegram-note");
    const box = $("#telegram-actions");
    if (!box) return;
    let state;
    try { state = await (await fetch("/api/telegram")).json(); } catch { return; }
    box.replaceChildren();
    if (!state.configured) {
      note.textContent = "Proposals and approvals on your phone. This agent has no bot yet: the team adds a BotFather token on the server.";
      box.append(el("span", { class: "pill plain", text: "Not set up" }));
      return;
    }
    if (state.linked) {
      note.textContent = `Linked to ${state.chat_name ? "@" + state.chat_name : "your chat"} on @${state.bot}. Write to the bot, or tap Approve on the cards it sends.`;
      box.append(sourceBadge("telegram"), el("button", { type: "button", class: "quiet", text: "Unlink", onclick: async () => { await fetch("/api/telegram/unlink", { method: "POST" }); drawTelegram(); } }));
      return;
    }
    note.textContent = `Bot @${state.bot}. Link your chat: the link is good for 15 minutes and works once.`;
    box.append(el("button", { type: "button", class: "primary small", text: "Link Telegram", onclick: async () => {
      const made = await (await fetch("/api/telegram/link", { method: "POST" })).json();
      box.replaceChildren(
        el("a", { class: "primary small", href: made.url, target: "_blank", rel: "noopener", text: "Open in Telegram" }),
        el("button", { type: "button", class: "quiet", text: "Copy link", onclick: async () => { try { await navigator.clipboard.writeText(made.url); savedNote("Link copied"); } catch {} } }));
      note.textContent = "Open the link, press Start in Telegram, and come back: this row will say Linked.";
      // Watch for the link to land.
      const poll = setInterval(async () => {
        const now = await (await fetch("/api/telegram")).json();
        if (now.linked) { clearInterval(poll); drawTelegram(); savedNote("Telegram linked ✓"); }
      }, 3000);
      setTimeout(() => clearInterval(poll), 15 * 60 * 1000);
    } }));
  }

  // Voice: the number to call, or no number yet.
  async function drawVoice() {
    const note = $("#voice-note");
    const box = $("#voice-actions");
    if (!box) return;
    let state;
    try { state = await (await fetch("/api/voice")).json(); } catch { return; }
    box.replaceChildren();
    if (!state.configured) {
      note.textContent = "Call your agent and approve on the call. This agent has no number yet: the team sets one up on the server.";
      box.append(el("span", { class: "pill plain", text: "Not set up" }));
      return;
    }
    note.textContent = `Call ${state.display}. It searches, proposes and, when you say yes on the call, buys. Your words go into the record.`;
    box.append(sourceBadge("voice"), el("a", { class: "primary small", href: `tel:${state.number}`, text: `Call ${state.display}` }));
  }

  function showMemory(memory, history = []) {
    memoryState = { profile: memory.profile ?? {}, rules: { max_total: null, refundable_only: false, avoid_categories: [], ...(memory.rules ?? {}) }, presentation: { shortlist: 1, lead: "photo", detail: "full", language: "en", ...(memory.presentation ?? {}) }, preferences: memory.preferences ?? [] };
    const root = $("#settings-body");
    for (const key of ["city", "party", "note"]) root.querySelector(`[name=${key}]`).value = memoryState.profile[key] ?? "";
    root.querySelector("[name=max_total]").value = memoryState.rules.max_total != null ? memoryState.rules.max_total / 100 : "";
    root.querySelector("[name=refundable_only]").checked = Boolean(memoryState.rules.refundable_only);
    for (const b of root.querySelectorAll("#avoid .toggle")) b.classList.toggle("on", memoryState.rules.avoid_categories.includes(b.dataset.value));
    for (const key of ["shortlist", "lead", "detail", "language"]) seg(key, memoryState.presentation[key]);
    let theme = "system"; try { theme = localStorage.getItem("agent.theme") || "system"; } catch {}
    seg("theme", theme);
    drawInForce();
    $("#prefs").replaceChildren(...(memoryState.preferences.length ? memoryState.preferences.map((fact) => el("li", {}, el("span", { text: fact }),
      el("button", { type: "button", class: "forget", title: "Forget this", "aria-label": `Forget: ${fact}`, text: "×", onclick: async () => {
        const updated = await (await fetch("/api/memory/forget", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fact }) })).json();
        showMemory(updated, history);
      } }))) : [el("li", { class: "muted empty", text: "Nothing yet. Tell your agent what you like and it will keep it." })]));
    $("#recent-history").replaceChildren(...(history.length ? history.map((h) => el("li", { text: h })) : [el("li", { class: "muted empty", text: "No purchases yet." })]));
    drawPreview();
    drawUsedBy();
    drawTelegram();
    drawVoice();
  }

  async function loadMemory() {
    const memory = await (await fetch("/api/memory")).json();
    showMemory(memory, memory.history);
  }

  // Changes: text fields save a moment after typing; the rest at once.
  $("#settings-body").addEventListener("input", (event) => {
    const input = event.target;
    if (!memoryState || !input.name) return;
    if (["city", "party", "note"].includes(input.name)) memoryState.profile[input.name] = input.value;
    else if (input.name === "max_total") { const v = input.value.trim(); memoryState.rules.max_total = v ? Math.round(Number(v) * 100) : null; }
    else if (input.name === "refundable_only") memoryState.rules.refundable_only = input.checked;
    else return;
    drawInForce();
    scheduleSave();
  });
  $("#settings-body").addEventListener("click", (event) => {
    const toggle = event.target.closest("#avoid .toggle");
    if (toggle && memoryState) {
      toggle.classList.toggle("on");
      memoryState.rules.avoid_categories = [...document.querySelectorAll("#avoid .toggle.on")].map((b) => b.dataset.value);
      drawInForce();
      scheduleSave();
      return;
    }
    if (event.target.closest("#look-reset") && memoryState) {
      memoryState.presentation = { shortlist: 1, lead: "photo", detail: "full", language: "en" };
      for (const key of ["shortlist", "lead", "detail", "language"]) seg(key, memoryState.presentation[key]);
      applyTheme("system"); seg("theme", "system");
      drawPreview();
      scheduleSave();
      return;
    }
    const option = event.target.closest(".seg button");
    if (option && memoryState) {
      const name = option.parentElement.dataset.setting;
      const value = option.dataset.value;
      seg(name, value);
      if (name === "theme") { applyTheme(value); return; }
      memoryState.presentation[name] = name === "shortlist" ? Number(value) : value;
      drawPreview();
      scheduleSave();
    }
  });
  // The blocks: scroll to one from the side navigation; mark the one in view.
  $("#settings-nav").addEventListener("click", (event) => {
    const button = event.target.closest("[data-part]");
    if (!button) return;
    document.getElementById(`part-${button.dataset.part}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  const partWatcher = new IntersectionObserver((entries) => {
    const seen = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (!seen) return;
    for (const b of document.querySelectorAll("#settings-nav button")) b.classList.toggle("on", b.dataset.part === seen.target.dataset.part);
  }, { root: null, rootMargin: "-20% 0px -60% 0px", threshold: 0 });
  for (const part of document.querySelectorAll(".s-card[data-part]")) partWatcher.observe(part);

  // ---- Guides: a panel with the steps for each thing to set up

  function openGuide(name) {
    const g = window.GUIDES?.[name];
    const dialog = $("#guide");
    if (!g || !dialog) return;
    const list = (items, cls) => el("ol", { class: cls }, items.map(([title, text]) => el("li", {}, el("b", { text: title }), el("p", { text }))));
    $("#guide-body").replaceChildren(
      el("header", {},
        el("div", {}, el("p", { class: "eyebrow", text: "Guide" }), el("h2", { id: "guide-title", text: g.title }), el("p", { class: "lead", text: g.lead })),
        el("button", { class: "close", type: "button", "aria-label": "Close", text: "×", onclick: () => dialog.close() })),
      g.steps?.length && el("h3", { text: "Steps" }), g.steps?.length && list(g.steps, "g-steps"),
      g.notes?.length && el("h3", { text: "Good to know" }), g.notes?.length && el("ul", { class: "g-notes" }, g.notes.map((n) => el("li", { text: n }))),
      g.trouble?.length && el("h3", { text: "If something's off" }), g.trouble?.length && list(g.trouble, "g-trouble"),
      el("p", { class: "guide-link" }, "This guide: ", el("a", { href: `/guides/${name}`, text: `${location.origin}/guides/${name}` })));
    if (!dialog.open) dialog.showModal();
    history.replaceState(null, "", `/guides/${name}`);
  }
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-guide]");
    if (!link) return;
    event.preventDefault();
    openGuide(link.dataset.guide);
  });
  $("#guide").addEventListener("click", (event) => { if (event.target === event.currentTarget) event.currentTarget.close(); });
  $("#guide").addEventListener("close", () => { if (location.pathname.startsWith("/guides/")) history.replaceState(null, "", "/"); });

  function showView(name) {
    for (const node of document.querySelectorAll(".view")) node.hidden = node.dataset.view !== name;
    for (const button of document.querySelectorAll("[data-view].row")) button.classList.toggle("on", button.dataset.view === name);
    if (name === "purchases") loadPurchases();
    if (name === "approvals") refreshApprovals();
    if (name === "history") loadHistory();
    if (name === "memory") loadMemory();
    try { localStorage.setItem("agent.view", name); } catch {}
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-view].row");
    if (button) showView(button.dataset.view);
  });
  $("[data-refresh-purchases]").addEventListener("click", loadPurchases);
  $("[data-refresh-approvals]").addEventListener("click", refreshApprovals);
  $("[data-refresh-history]").addEventListener("click", loadHistory);
  $("#history-chips").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    historyKind = chip.dataset.kind;
    for (const c of document.querySelectorAll("#history-chips .chip")) c.classList.toggle("on", c === chip);
    drawHistory();
  });
  $("#source-chips").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    historySource = chip.dataset.source;
    for (const c of document.querySelectorAll("#source-chips .chip")) c.classList.toggle("on", c === chip);
    drawHistory();
  });

  // The coins in the sidebar: read when the page opens, and kept fresh.
  async function refreshWallets() {
    try { wallets(await (await fetch("/api/wallets")).json()); } catch {}
  }
  setInterval(refreshWallets, 30000);

  // Another brain (over MCP) may add events while this page is idle: look
  // for them every few seconds.
  let boot = null;   // the agent process the thread was drawn from
  async function catchUp() {
    if (busy) return;
    try {
      const state = await (await fetch("/api/state")).json();
      if (boot && state.boot && state.boot !== boot) {
        // The agent restarted (a deploy): its numbers start over, and the
        // journal is the truth. Draw the thread again from it.
        boot = state.boot;
        await rebuildThread();
        return;
      }
      const before = lastSeq;
      state.events.forEach(apply);
      approvalsBadge(state.pending_approvals ?? 0);
      if (lastSeq !== before && !$("[data-view=approvals]").hidden) refreshApprovals();
    } catch {}
  }
  setInterval(catchUp, 4000);

  // ---- Start: who the agent is, and the conversation so far

  (async () => {
    const state = await (await fetch("/api/state")).json();
    capsInForce = state.caps ?? null;
    document.title = state.name;
    for (const node of document.querySelectorAll("[data-name]")) node.textContent = state.name;
    $("[data-customer]").textContent = state.customer.full_name;
    $("[data-initials]").textContent = state.customer.full_name.split(/\s+/).map((word) => word[0]).join("").slice(0, 2).toUpperCase();
    $("[data-card]").textContent = state.card;
    $("#mcp-url").textContent = state.mcp_url ?? "";
    $("#copy-mcp").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(state.mcp_url); $("#copy-mcp").textContent = "Copied"; } catch { $("#copy-mcp").textContent = "Select and copy"; }
      setTimeout(() => { $("#copy-mcp").textContent = "Copy"; }, 2000);
    });
    // Say what decides: a model by its name, or the scripted stand-in.
    const brain = $("[data-brain]");
    brain.textContent = state.brain.name;
    brain.title = state.brain.is_model
      ? "A language model decides what to search and propose"
      : "No model connected: fixed rules decide what to search and propose";
    for (const shop of state.shops) shops.set(shop.id, shop);
    drawShops();
    refreshWallets();
    boot = state.boot ?? null;
    await loadThread();
    // Live events the journal doesn't keep (the coins) and anything newer.
    state.events.forEach(apply);
    closeSteps();
    readySend();
    approvalsBadge(state.pending_approvals ?? 0);
    // A link from another assistant lands on the approvals, on its proposal.
    const guide = location.pathname.match(/^\/guides\/([a-z]+)/);
    if (guide) { showView("memory"); openGuide(guide[1]); }
    const route = location.pathname.match(/^\/approvals(?:\/([A-Za-z0-9]+))?/);
    if (route) {
      focusProposal = route[1] ?? null;
      showView("approvals");
      history.replaceState(null, "", "/");
    } else {
      box.focus();
    }
  })();
  // Who is signed in, and the way out. An app running open shows nothing.
  fetch("/api/whoami").then((r) => r.json()).then((me) => {
    const who = document.getElementById("who");
    if (!who || !me.user) return;
    who.querySelector("[data-user]").textContent = me.user;
    who.hidden = false;
  }).catch(() => {});
})();
