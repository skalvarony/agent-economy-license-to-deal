// Storefront behaviour: updates the page in place instead of reloading it.
//
// Every action is a plain HTML form that also works without this script. Forms
// marked data-async are sent with `Accept: application/json`; the server then
// answers with the HTML to swap in rather than a redirect.
(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const JSON_HEADERS = { Accept: "application/json" };
  // Motion (vendor/motion.js) drives the animations that need physics or
  // geometry. Without it, or for people who asked for less motion, the page
  // works the same and simply doesn't move.
  const lively = document.documentElement.classList.contains("motion");
  const motion = lively ? window.Motion : null;
  const SWIFT = [0.2, 0.8, 0.2, 1];

  function money(cents) {
    return `$${(cents / 100).toFixed(2)}`.replace(/\.00$/, "");
  }

  // Restart a one-shot CSS animation by toggling its class.
  function replay(node, name) {
    node.classList.remove(name);
    void node.offsetWidth;
    node.classList.add(name);
  }

  function escapeHtml(text) {
    const node = document.createElement("span");
    node.textContent = text ?? "";
    return node.innerHTML;
  }

  function toast(text, bad = false) {
    const note = document.createElement("p");
    note.className = bad ? "toast bad" : "toast";
    note.textContent = text;
    $("#toasts").append(note);
    setTimeout(() => note.remove(), 2600);
  }

  // Show a button as working, or restore it.
  function busy(button, on) {
    if (!button || !button.isConnected) return;
    if (on) {
      button.dataset.label = button.textContent;
      if (button.dataset.busy) button.textContent = button.dataset.busy;
    } else if (button.dataset.label) {
      button.textContent = button.dataset.label;
    }
    button.disabled = on;
  }

  async function send(form, submitter) {
    const body = new URLSearchParams(new FormData(form));
    if (submitter?.name) body.set(submitter.name, submitter.value);
    const response = await fetch(form.action, {
      method: "POST",
      headers: {
        ...JSON_HEADERS,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body,
    });
    return { ok: response.ok, data: await response.json() };
  }

  // ---- Cart: the drawer, the badge and the cart page share one renderer.

  const onCartPage = location.pathname === "/cart";

  function showCart(cart) {
    const badge = $("#cart-badge");
    if (String(cart.count) !== badge.textContent) replay(badge, "pop");
    badge.textContent = cart.count;
    badge.hidden = !cart.count;
    for (const list of $$("[data-cart-lines]")) {
      // Lines only make an entrance when the cart is first shown.
      list.classList.add("settled");
      list.innerHTML = cart.lines_html;
    }
    for (const sums of $$("[data-cart-summary]")) {
      sums.innerHTML = cart.summary_html;
    }
    for (const code of $$("[data-cart-promo]")) code.value = cart.promo_code;
    for (const empty of $$("[data-cart-empty]")) empty.hidden = cart.count > 0;
    for (const filled of $$("[data-cart-filled]")) filled.hidden = !cart.count;
  }

  function openCart() {
    $("#drawer [data-cart-lines]").classList.remove("settled");
    document.body.classList.add("cart-open");
    $("#drawer").setAttribute("aria-hidden", "false");
  }

  function closeCart() {
    document.body.classList.remove("cart-open");
    $("#drawer").setAttribute("aria-hidden", "true");
  }

  $("#cart-link").addEventListener("click", async (event) => {
    if (onCartPage) return;
    event.preventDefault();
    openCart();
    const response = await fetch("/cart", { headers: JSON_HEADERS });
    showCart(await response.json());
  });
  $("#backdrop").addEventListener("click", closeCart);
  $("#drawer-close").addEventListener("click", closeCart);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeCart();
  });

  // ---- Paying: stay on the page for a declined card or a changed total.

  // The shop's total is no longer the one on the page: ask before paying it.
  function askAgain(form, answer) {
    const dialog = $("#changed");
    $("[data-old]", dialog).textContent = answer.seen_total;
    $("[data-new]", dialog).textContent = answer.new_total;
    $("[data-breakdown]", dialog).innerHTML = answer.breakdown
      .map(([label, amount]) =>
        `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(amount)}</dd>`)
      .join("");
    const accept = $("[data-accept]", dialog);
    accept.textContent = `Accept and pay ${answer.new_total}`;
    accept.onclick = () => {
      dialog.close();
      form.elements.seen_total.value = answer.new_total_cents;
      form.requestSubmit();
    };
    // Declining reloads the page, which now shows the new total.
    $("[data-decline]", dialog).onclick = () => location.reload();
    dialog.showModal();
  }

  // Returns true when the browser is leaving the page.
  function showPayment(form, answer) {
    const error = $("[data-pay-error]", form);
    error.hidden = true;
    // Paid, or the session ended and the shopper has to sign in again.
    if (answer.redirect) {
      location.assign(answer.redirect);
      return true;
    }
    if (answer.code === "changed" && answer.new_total) {
      askAgain(form, answer);
      return false;
    }
    // A card that failed is spent; the next try tokenises it again.
    if (form.elements.payment_method) form.elements.payment_method.value = "";
    error.textContent = answer.message;
    error.hidden = false;
    return false;
  }

  // ---- Stripe's card fields, when the shop's rail is Stripe. The card
  // never reaches the shop: the browser turns it into a PaymentMethod id.

  const stripeBox = $("[data-stripe]");
  let stripeCard = null;
  if (stripeBox && window.Stripe) {
    const stripe = Stripe(stripeBox.dataset.stripe);
    // The fields live in Stripe's frame, so the page's colours are passed in.
    const theme = getComputedStyle(document.documentElement);
    const card = stripe.elements().create("card", {
      hidePostalCode: true,
      style: {
        base: {
          fontSize: "16px",
          color: theme.getPropertyValue("--text").trim(),
          "::placeholder": { color: theme.getPropertyValue("--muted").trim() },
        },
      },
    });
    card.mount("#card-element");
    stripeCard = { stripe, card };
  }

  // Returns a message when the card can't be used; null when it's ready.
  async function tokeniseCard(form) {
    const field = form.elements.payment_method;
    if (!stripeCard || !field || field.value) return null;
    const { paymentMethod, error } = await stripeCard.stripe
      .createPaymentMethod({ type: "card", card: stripeCard.card });
    if (error) return error.message;
    field.value = paymentMethod.id;
    return null;
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    const kind = form.dataset.async;
    if (!kind) return;
    event.preventDefault();
    const button = event.submitter || $("button.primary", form);
    busy(button, true);
    let leaving = false;
    try {
      if (kind === "pay") {
        const problem = await tokeniseCard(form);
        if (problem) {
          showPayment(form, { message: problem });
          return;
        }
      }
      const { ok, data } = await send(form, event.submitter);
      if (kind === "pay") {
        leaving = showPayment(form, data);
      } else if (!ok) {
        toast(data.message, true);
      } else {
        showCart(data);
        if (data.message) toast(data.message, data.bad);
        if (data.message && !data.bad && !onCartPage) openCart();
      }
    } catch {
      toast("Something went wrong. Try again.", true);
    } finally {
      if (!leaving) busy(button, false);
    }
  });

  // ---- Small controls: quantity steppers, copy buttons, saved deals.

  document.addEventListener("click", async (event) => {
    const step = event.target.closest("[data-step]");
    if (step) {
      const input = $("input", step.closest("[data-stepper]"));
      const next = Number(input.value) + Number(step.dataset.step);
      input.value = Math.min(Math.max(next, input.min), input.max);
      return;
    }

    const copy = event.target.closest("[data-copy], [data-copy-from]");
    if (copy) {
      const text = copy.dataset.copy
        ?? $(`#${copy.dataset.copyFrom}`).textContent;
      await navigator.clipboard.writeText(text);
      toast("Copied");
      return;
    }

    const heart = event.target.closest("[data-fav]");
    if (heart) {
      event.preventDefault();
      const id = heart.dataset.fav;
      saved.has(id) ? saved.delete(id) : saved.add(id);
      try {
        localStorage.setItem(SAVED_KEY, JSON.stringify([...saved]));
      } catch {
        // Private windows may refuse storage; the heart still toggles.
      }
      paintSaved();
      if (saved.has(id)) replay(heart, "pop");
      toast(saved.has(id) ? "Saved" : "Removed from saved");
      if (deals) filterDeals();
    }
  });

  // Deal page: each option has its own price, stock and item to buy.
  document.addEventListener("change", (event) => {
    const choice = event.target;
    if (!choice.matches('.options input[name="product_id"]')) return;
    const shown = choice.dataset;
    const price = $("[data-option-price]");
    const from = Number(price.dataset.cents);
    price.dataset.cents = shown.cents;
    price.innerHTML = shown.price;
    if (motion) {
      const figure = $("strong", price);
      motion.animate(from, Number(shown.cents), {
        duration: 0.45,
        ease: SWIFT,
        onUpdate: (cents) => { figure.textContent = money(Math.round(cents)); },
      });
    }
    $("[data-option-save]").textContent = shown.save;
    $("[data-option-left]").textContent = shown.left;
    $("[data-option-promo]").innerHTML = shown.promo;
    $("[data-option-coins]").textContent = shown.coins;
    for (const block of $$("[data-includes]")) {
      block.hidden = block.dataset.includes !== choice.value;
      if (motion && !block.hidden) {
        motion.animate(block, { opacity: [0, 1], y: [10, 0] }, { duration: 0.35 });
      }
    }
    const quantity = $('.add input[name="quantity"]');
    quantity.max = shown.max;
    quantity.value = Math.min(quantity.value, shown.max);
    const ask = $("#ask");
    ask.textContent = ask.dataset.template
      .replace("{title}", shown.title)
      .replace("{id}", choice.value);
    $("[data-option-id]").textContent = choice.value;
  });

  // Saved deals live in this browser, one list per shop.
  const SAVED_KEY = `saved:${location.host}`;
  let saved = new Set();
  try {
    saved = new Set(JSON.parse(localStorage.getItem(SAVED_KEY) || "[]"));
  } catch {
    // No storage: start with nothing saved.
  }

  function paintSaved() {
    for (const heart of $$("[data-fav]")) {
      heart.setAttribute("aria-pressed", saved.has(heart.dataset.fav));
    }
  }
  paintSaved();

  // ---- Home: search, category, saved and sort, all kept in the URL.

  const deals = $("#deals");
  const search = $("#q");
  const params = new URLSearchParams(location.search);
  let category = params.get("category") || "";

  function filterDeals() {
    const cards = $$(".card", deals);
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const card of cards) {
      const inCategory = !category
        || (category === "saved"
          ? saved.has(card.dataset.id)
          : card.dataset.category === category);
      const match = inCategory
        && words.every((word) => card.dataset.search.includes(word));
      card.hidden = !match;
      shown += match;
    }

    // Cheapest and catalogue order go up; saving and rating go down.
    const key = $("#sort").value;
    const direction = key === "position" || key === "price" ? 1 : -1;
    let place = 0;
    cards
      .sort((a, b) => direction * (a.dataset[key] - b.dataset[key]))
      .forEach((card) => {
        // Re-inserting a card replays its entrance, staggered by position.
        if (!card.hidden) card.style.setProperty("--i", place++);
        deals.append(card);
      });

    $("#count").textContent = shown;
    $("#empty").hidden = shown > 0;
    for (const chip of $$(".chip")) {
      chip.classList.toggle("on", chip.dataset.filter === category);
    }

    const next = new URLSearchParams();
    if (search.value) next.set("q", search.value);
    if (category) next.set("category", category);
    if (key !== "position") next.set("sort", key);
    const query = next.toString();
    history.replaceState(null, "", query ? `?${query}` : location.pathname);
  }

  if (deals) {
    search.value = params.get("q") || "";
    $("#sort").value = params.get("sort") || "position";
    search.addEventListener("input", filterDeals);
    search.form.addEventListener("submit", (event) => event.preventDefault());
    $("#sort").addEventListener("change", filterDeals);
    for (const chip of $$(".chip")) {
      chip.addEventListener("click", () => {
        category = chip.dataset.filter;
        filterDeals();
      });
    }
    filterDeals();
  } else {
    // ---- Other pages: suggest deals while typing; Enter goes to the list.
    const list = $("#suggest");
    let timer;

    async function suggest() {
      const query = search.value.trim();
      if (!query) {
        list.hidden = true;
        return;
      }
      const response = await fetch(`/search?q=${encodeURIComponent(query)}`);
      const items = await response.json();
      list.innerHTML = items.length
        ? items.map((item) => `
            <li><a href="${escapeHtml(item.url)}">
              ${item.image
                ? `<img src="${escapeHtml(item.image)}" alt="">`
                : '<span class="noimg"></span>'}
              <span><b>${escapeHtml(item.title)}</b>
                <small>${escapeHtml(item.where)}${
                  item.sold_out ? " · Sold out" : ""}</small></span>
              <strong>${escapeHtml(item.price)}</strong>
            </a></li>`).join("")
        : `<li class="none">No deals match “${escapeHtml(query)}”.</li>`;
      list.hidden = false;
    }

    search.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(suggest, 150);
    });
    search.addEventListener("keydown", (event) => {
      const links = $$("a", list);
      const current = links.findIndex((a) => a.classList.contains("active"));
      if (event.key === "Escape") list.hidden = true;
      if (!links.length || !["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) {
        return;
      }
      if (event.key === "Enter") {
        if (current < 0) return;  // No pick: submit the search to the list.
        event.preventDefault();
        location.assign(links[current].href);
        return;
      }
      event.preventDefault();
      const move = event.key === "ArrowDown" ? 1 : -1;
      const next = (current + move + links.length) % links.length;
      links.forEach((a, index) => a.classList.toggle("active", index === next));
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".search")) list.hidden = true;
    });
  }

  // ---- Details: a shadow under the header once the page has scrolled.
  const top = $("#top");
  const onScroll = () => top.classList.toggle("scrolled", scrollY > 8);
  addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // The featured deal leans towards the pointer.
  const hero = $(".hero");
  const featured = $(".featured");
  if (lively && hero && featured) {
    hero.addEventListener("pointermove", (event) => {
      const box = hero.getBoundingClientRect();
      const x = (event.clientX - box.left) / box.width - 0.5;
      const y = (event.clientY - box.top) / box.height - 0.5;
      featured.style.transform =
        `perspective(900px) rotateY(${x * 10}deg) rotateX(${-y * 8}deg)`;
    });
    hero.addEventListener("pointerleave", () => {
      featured.style.transform = "";
    });
  }

  // Deal page: the small photos switch the big one.
  document.addEventListener("click", (event) => {
    const pick = event.target.closest("[data-photo]");
    if (!pick) return;
    const main = $(".gallery .photo img");
    for (const other of $$("[data-photo]")) other.removeAttribute("aria-current");
    pick.setAttribute("aria-current", "true");
    main.src = pick.dataset.photo;
    if (motion) {
      motion.animate(main, { opacity: [0.2, 1], scale: [1.03, 1] }, { duration: 0.4 });
    }
  });

  // An order just paid for: throw a little confetti from the confirmation.
  const celebrate = $("[data-celebrate]");
  if (celebrate && motion) {
    const origin = ($(".notice.good") || celebrate).getBoundingClientRect();
    const colours = ["var(--accent)", "#e8a317", "#0f7a4d", "#e0245e"];
    for (let n = 0; n < 36; n++) {
      const bit = document.createElement("i");
      bit.className = "confetti";
      bit.style.background = colours[n % colours.length];
      bit.style.left = `${origin.left + origin.width * Math.random()}px`;
      bit.style.top = `${origin.top + origin.height / 2}px`;
      document.body.append(bit);
      motion.animate(bit, {
        x: (Math.random() - 0.5) * 420,
        y: [0, -140 - Math.random() * 160, 380 + Math.random() * 200],
        rotate: (Math.random() - 0.5) * 900,
        opacity: [1, 1, 0],
      }, { duration: 1.6 + Math.random() * 0.8, ease: "easeOut" })
        .then(() => bit.remove());
    }
  }

  // ---- Order page: follow the voucher as the merchant or a refund changes it.

  if ($("[data-live-order]")) {
    let last = "";
    setInterval(async () => {
      const response = await fetch(location.pathname, { headers: JSON_HEADERS });
      if (!response.ok) return;
      const order = await response.json();
      const snapshot = JSON.stringify(order);
      if (last && snapshot !== last) toast("Your order was updated");
      last = snapshot;
      $("[data-paid]").textContent = order.paid;
      for (const [id, voucher] of Object.entries(order.vouchers)) {
        const pill = $(`[data-voucher="${id}"]`);
        if (!pill) continue;
        pill.textContent = voucher.state;
        pill.className = `pill ${voucher.style}`;
      }
    }, 3000);
  }
})();
