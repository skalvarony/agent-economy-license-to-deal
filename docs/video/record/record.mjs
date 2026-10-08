// Records the demo film's footage from the real local stack.
//
//   docs/video/record/reset.sh          # clean local state first (local only)
//   node docs/video/record/record.mjs   # writes record/out/<take>/frames + frames.json + markers.json
//   node docs/video/record/encode.mjs   # frames -> ../project/assets/<take>.mp4 (constant 30 fps)
//
// Each take is a CDP screencast of a 1600x900 viewport at deviceScaleFactor 2
// (3200x1800 frames, i.e. exactly 16:9). Frames keep their real timestamps;
// encode.mjs turns them into constant-30-fps H.264. Only the page is captured,
// never Chrome's UI. A small cursor is drawn inside the page (Playwright draws
// none) and follows the real mouse events; a press shows a soft ring.
// Pacing is human: typing 40-70 ms per character, eased mouse paths, a pause
// before each click. markers.json in each take logs when things happened, in
// seconds from the take's first frame; the composition cuts on those.
//
// Order of takes (so every take shows a believable state):
//   ask      spa: type, proposal, approve, receipt
//   beer     (off camera: the three-best preference) type, shortlist, approve
//   shop     (off camera: sign in) My vouchers -> the spa order -> who bought it
//   console  Orders -> the spa order (payment, agent's side) -> the beer order -> Refund
//   venue    the spa arrival -> honour -> used
//   after    agent: Your purchases (used + refunded) -> the spa -> Evidence
import { chromium } from "../../../tests-e2e/node_modules/playwright-core/index.mjs";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = process.env.OUT || path.join(HERE, "out");
const ONLY = process.env.TAKES ? process.env.TAKES.split(",") : null;
const AGENT = "http://localhost:8190";
const SHOP = "http://localhost:8181";
const CONSOLE = "http://localhost:8195";
const VENUE = "http://localhost:8196";
const [, EMAIL, PASSWORD] = fs.readFileSync(path.join(HERE, "../../../shops/a/users.csv"), "utf8").split("\n")[1].trim().split(",");

// ---- deterministic "human" jitter
let seed = 7;
const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
const between = (a, b) => a + (b - a) * rnd();

// ---- the cursor drawn in the page, with a soft ring on press
const CURSOR = `
(() => {
  const draw = () => {
    if (document.getElementById("__cur")) return;
    const c = document.createElement("div");
    c.id = "__cur";
    c.innerHTML = '<svg width="24" height="24" viewBox="0 0 26 26"><path d="M5 3.2v17.2l4.3-4.1 2.9 6.6 3-1.3-2.9-6.5h6z" fill="#0d0d0d" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    Object.assign(c.style, { position: "fixed", left: "0", top: "0", width: "24px", height: "24px", zIndex: "2147483647",
      pointerEvents: "none", opacity: "0", transformOrigin: "5px 3px", transition: "scale .1s ease-out",
      filter: "drop-shadow(0 1px 1.5px rgb(0 0 0 / 22%))" });
    const ring = document.createElement("div");
    ring.id = "__ring";
    Object.assign(ring.style, { position: "fixed", left: "0", top: "0", width: "34px", height: "34px", marginLeft: "-17px", marginTop: "-17px",
      borderRadius: "50%", border: "2px solid rgb(13 13 13 / 35%)", zIndex: "2147483646", pointerEvents: "none", opacity: "0", scale: "0.4" });
    document.documentElement.append(ring, c);
    const at = (e) => { c.style.opacity = "1"; c.style.translate = (e.clientX - 5) + "px " + (e.clientY - 3) + "px"; };
    addEventListener("mousemove", at, true);
    addEventListener("mousedown", (e) => {
      at(e); c.style.scale = "0.86";
      ring.style.translate = e.clientX + "px " + e.clientY + "px";
      ring.animate([{ opacity: 0.9, scale: 0.4 }, { opacity: 0, scale: 1.25 }], { duration: 520, easing: "cubic-bezier(.2,.7,.3,1)" });
    }, true);
    addEventListener("mouseup", (e) => { at(e); c.style.scale = "1"; }, true);
  };
  if (document.documentElement) draw();
  addEventListener("DOMContentLoaded", draw);
})();`;

const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" });
const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 2, locale: "en-US" });
await ctx.addInitScript(CURSOR);
const page = await ctx.newPage();
const mouse = { x: 1000, y: 600 };

const wait = (ms) => page.waitForTimeout(ms);

async function showCursor() {
  await page.mouse.move(mouse.x + 1, mouse.y);
  await page.mouse.move(mouse.x, mouse.y);
}

// An eased path with a slight arc, one mouse event per frame.
async function moveTo(x, y, ms) {
  const x0 = mouse.x, y0 = mouse.y;
  const dist = Math.hypot(x - x0, y - y0);
  if (dist < 1) return;
  ms = ms ?? Math.min(1100, 420 + dist * 0.5);
  const bend = (rnd() < 0.5 ? -1 : 1) * Math.min(50, dist * 0.07);
  const nx = -(y - y0) / dist, ny = (x - x0) / dist;
  const steps = Math.max(8, Math.round(ms / 16));
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    const e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    const arc = Math.sin(Math.PI * e) * bend;
    mouse.x = x0 + (x - x0) * e + nx * arc;
    mouse.y = y0 + (y - y0) * e + ny * arc;
    await page.mouse.move(mouse.x, mouse.y);
    await wait(16);
  }
  mouse.x = x; mouse.y = y;
  await page.mouse.move(x, y);
}

async function pointAt(locator, { dx = 0.5, dy = 0.5, ms } = {}) {
  await locator.waitFor({ state: "visible" });
  const b = await locator.boundingBox();
  await moveTo(b.x + b.width * dx, b.y + b.height * dy, ms);
}

async function click(locator, opts = {}) {
  await pointAt(locator, opts);
  await wait(between(280, 420));
  await page.mouse.down();
  await wait(between(80, 120));
  await page.mouse.up();
}

async function type(text, [lo, hi] = [40, 70]) {
  for (const ch of text) {
    await page.keyboard.type(ch);
    await wait(between(lo, hi) + (ch === " " ? between(10, 40) : 0) + (/[,]/.test(ch) ? 140 : 0));
  }
}

// Smoothly scroll the nearest scroller of `locator`'s element so that element sits `atY` px
// from the scroller's top; or by a plain delta with { by }; or to { to: 0 | "bottom" }.
async function scrollTo(locator, { atY = 80, by, to, ms = 1800 } = {}) {
  await locator.first().evaluate(async (el, { atY, by, to, ms }) => {
    const pick = (n0) => { for (let n = n0; n; n = n.parentElement) { const s = getComputedStyle(n); if (/(auto|scroll)/.test(s.overflowY) && n.scrollHeight > n.clientHeight + 2) return n; } return document.scrollingElement; };
    const sc = pick(el);
    const from = sc.scrollTop;
    let dest;
    if (to === "bottom") dest = sc.scrollHeight - sc.clientHeight;
    else if (typeof to === "number") dest = to;
    else if (by != null) dest = from + by;
    else {
      const top = sc === document.scrollingElement ? 0 : sc.getBoundingClientRect().top;
      dest = from + (el.getBoundingClientRect().top - top) - atY;
    }
    dest = Math.max(0, Math.min(dest, sc.scrollHeight - sc.clientHeight));
    const prev = sc.style.scrollBehavior;
    sc.style.scrollBehavior = "auto";
    await new Promise((done) => {
      const t0 = performance.now();
      const tick = () => {
        const p = Math.min(1, (performance.now() - t0) / ms);
        const e = p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
        sc.scrollTop = from + (dest - from) * e;
        p < 1 ? requestAnimationFrame(tick) : done();
      };
      requestAnimationFrame(tick);
    });
    sc.style.scrollBehavior = prev;
  }, { atY, by, to, ms });
}

// ---- takes: a screencast with real timestamps and a markers log
async function take(name, body) {
  if (ONLY && !ONLY.includes(name)) return;
  const dir = path.join(OUT, name);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(path.join(dir, "frames"), { recursive: true });
  const cdp = await ctx.newCDPSession(page);
  const frames = [];
  const writes = [];
  cdp.on("Page.screencastFrame", (f) => {
    const file = `frames/${String(frames.length).padStart(5, "0")}.jpg`;
    frames.push({ file, t: f.metadata.timestamp });
    writes.push(fs.promises.writeFile(path.join(dir, file), Buffer.from(f.data, "base64")));
    cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId }).catch(() => {});
  });
  const markers = [];
  const mark = (label) => { markers.push({ label, t: Date.now() / 1000 }); console.log(`  ${name}: ${label}`); };
  await cdp.send("Page.startScreencast", { format: "jpeg", quality: 92, maxWidth: 3200, maxHeight: 1800, everyNthFrame: 1 });
  await wait(400);
  // nudge one frame so the take starts with a frame even on a still page
  await page.mouse.move(mouse.x + 0.5, mouse.y);
  await wait(100);
  mark("start");
  await body(mark);
  mark("end");
  await wait(300);
  await cdp.send("Page.stopScreencast");
  await Promise.all(writes);
  await cdp.detach();
  // Screencast frame timestamps are wall-clock seconds, like Date.now()/1000.
  const t0 = frames[0].t;
  fs.writeFileSync(path.join(dir, "frames.json"), JSON.stringify({ end: markers.at(-1).t - t0 + 0.2, frames: frames.map((f) => ({ ...f, t: f.t - t0 })) }));
  fs.writeFileSync(path.join(dir, "markers.json"), JSON.stringify(markers.map((m) => ({ label: m.label, t: +(m.t - t0).toFixed(3) })), null, 1));
  console.log(`${name}: ${frames.length} frames, ${(markers.at(-1).t - t0).toFixed(1)} s`);
}

async function open(url) {
  await page.goto(url, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  await showCursor();
}

async function setPresentation(shortlist) {
  const r = await fetch(AGENT + "/api/memory", { method: "PUT", headers: { "content-type": "application/json" },
    body: JSON.stringify({ presentation: { shortlist, lead: "photo", detail: "full", language: "en" } }) });
  if (!r.ok) throw new Error("memory: " + r.status);
}

// 1-2 · The ask, the proposal, Approve, the receipt (one continuous take)
if (!ONLY || ONLY.includes("ask")) { await setPresentation(1); await open(AGENT + "/"); }
await take("ask", async (mark) => {
  await wait(1500);
  await click(page.locator("#say"), { dx: 0.3 });
  await wait(400);
  mark("typing");
  await type("a spa day for two this Saturday, refundable, under $100");
  await wait(650);
  await click(page.locator("#composer button[type=submit]"));
  mark("sent");
  await page.locator(".feed .card.proposal").first().waitFor();
  mark("proposal");
  await wait(500);
  await moveTo(1460, 560, 900);
  await wait(900);
  // up to the top of the card: the title, the shop, the photos
  await scrollTo(page.locator(".feed .card.proposal"), { atY: 24, ms: 1700 });
  mark("card-top");
  await wait(2600);
  // down to the money
  await scrollTo(page.locator(".feed .card.proposal .sum"), { atY: 330, ms: 2600 });
  mark("total");
  await wait(2000);
  const approve = page.locator(".feed").getByRole("button", { name: /^Approve \$/ });
  mark("to-approve");
  await click(approve, { dx: 0.45 });
  mark("approve-click");
  await page.locator(".feed .receipt").first().waitFor();
  mark("receipt");
  await wait(1000);
  await moveTo(1460, 520, 900);
  await scrollTo(page.locator(".feed .receipt"), { atY: 120, ms: 1800 });
  mark("receipt-top");
  await wait(1200);
  await pointAt(page.locator(".feed .receipt .codes li, .feed .receipt code").first(), { dx: 1.25, dy: 0.5, ms: 900 });
  await wait(2600);
  mark("charged");
  await wait(2400);
});

// 3 · A second ask: the three best, approve one
if (!ONLY || ONLY.includes("beer")) { await setPresentation(3); await open(AGENT + "/"); }
await take("beer", async (mark) => {
  await wait(1200);
  await click(page.locator("#say"), { dx: 0.3 });
  await wait(300);
  mark("typing");
  await type("the three best beer tastings for two, under $40");
  await wait(500);
  await page.keyboard.press("Enter");
  mark("sent");
  const card = page.locator(".feed .card.proposal").last();
  await card.locator(".shortlist").waitFor();
  mark("proposal");
  await wait(400);
  await scrollTo(card.locator(".shortlist"), { atY: 110, ms: 1500 });
  mark("shortlist");
  await moveTo(900, 420, 900);
  await wait(1800);
  await click(card.getByRole("button", { name: /^Approve \$/ }), { dx: 0.45 });
  mark("approve-click");
  await page.locator(".feed .receipt").nth(1).waitFor();
  mark("receipt");
  await wait(600);
  await scrollTo(page.locator(".feed .receipt").last(), { atY: 120, ms: 1500 });
  await wait(2200);
});
if (!ONLY || ONLY.includes("beer")) await setPresentation(1);

// 4 · The shop knows who bought (signed in off camera)
if (!ONLY || ONLY.includes("shop")) {
  await page.goto(SHOP + "/login", { waitUntil: "networkidle" });
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel(/Password/).fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForLoadState("networkidle");
  await open(SHOP + "/vouchers");
}
await take("shop", async (mark) => {
  await wait(1200);
  const row = page.locator("a.o-row").first();
  await pointAt(row, { dx: 0.3, dy: 0.76, ms: 1000 });
  mark("row-hover");
  await wait(2000);
  await click(row, { dx: 0.3, dy: 0.3 });
  await page.waitForURL(/\/vouchers\/./);
  await page.waitForLoadState("networkidle");
  await showCursor();
  mark("order");
  await wait(1800);
  await moveTo(1420, 560, 900);
  await scrollTo(page.getByText("Who bought this", { exact: false }), { atY: 160, ms: 2400 });
  mark("who-bought");
  await wait(900);
  await pointAt(page.getByText(/^verified/).first(), { dx: 0.05, dy: 0.5, ms: 1000 });
  mark("signature");
  await wait(2600);
  await moveTo(1180, 640, 1000);
  mark("recorded");
  await wait(2200);
});

// 5 · The marketplace console: Orders, the spa's payment and agent side, then refund the beer tasting
if (!ONLY || ONLY.includes("console")) await open(CONSOLE + "/");
await take("console", async (mark) => {
  await wait(1000);
  await click(page.locator('#nav button[data-section="orders"]'));
  mark("orders");
  await page.locator("tbody tr[data-id]").first().waitFor();
  await wait(1500);
  await click(page.locator("tbody tr[data-id]", { hasText: "Spa" }).first(), { dx: 0.45 });
  mark("panel");
  await page.locator("#detail .panel-body:not(.skeleton)").first().waitFor();
  await wait(600);
  await moveTo(1480, 470, 900);
  mark("payment");
  await wait(2400);
  await scrollTo(page.locator("#detail").getByText("The agent's side", { exact: false }), { atY: 30, ms: 2400 });
  mark("agent-side");
  await wait(3000);
  await click(page.locator("tbody tr[data-id]", { hasText: "Beer" }).first(), { dx: 0.45 });
  mark("beer-panel");
  await page.locator("#detail .panel-body:not(.skeleton)").first().waitFor();
  await wait(1200);
  await click(page.locator("#detail button", { hasText: /^Refund$/ }).first());
  mark("refund-ask");
  await wait(700);
  const reason = page.locator("#detail input[name=reason]");
  if (await reason.count()) {
    await click(reason, { dx: 0.3 });
    await type("Plans changed", [40, 60]);
    await wait(400);
  }
  await click(page.locator("#detail button.primary", { hasText: /go ahead|Refund/ }).first());
  mark("refund-yes");
  await page.locator("tbody tr[data-id]", { hasText: "Refunded" }).first().waitFor({ timeout: 8000 }).catch(() => {});
  mark("refunded");
  await wait(600);
  await moveTo(820, 300, 1000);
  await wait(2600);
});

// 6 · The venue: the spa arrival, the ticket, honoured
if (!ONLY || ONLY.includes("venue")) await open(VENUE + "/");
await take("venue", async (mark) => {
  await wait(1300);
  await click(page.locator("tbody tr", { hasText: "Spa" }).first(), { dx: 0.45 });
  mark("ticket");
  await wait(600);
  await moveTo(1300, 470, 900);
  await wait(2200);
  const honour = page.locator("button.happen", { hasText: "Customer arrives, voucher honoured" });
  await click(honour, { dx: 0.4 });
  mark("honoured");
  await wait(900);
  await moveTo(1440, 380, 1000);
  await wait(3200);
});

// 7 · Back in the agent: Your purchases, the spa, the evidence
if (!ONLY || ONLY.includes("after")) await open(AGENT + "/");
await take("after", async (mark) => {
  await wait(1000);
  await click(page.locator('button[data-view="purchases"]'));
  mark("purchases");
  await wait(2600);
  await click(page.locator("button.p-row", { hasText: "Spa" }).first(), { dx: 0.3 });
  mark("spa");
  await wait(600);
  await moveTo(1300, 600, 900);
  await wait(2200);
  // down the ticket: what was paid, Approved · Web, the links
  const ev = page.locator("a.secondary", { hasText: "Evidence" }).first();
  await scrollTo(ev, { atY: 560, ms: 2400 });
  mark("paid");
  await wait(2200);
  await click(ev);
  await page.waitForURL(/evidence/);
  await page.waitForLoadState("networkidle");
  await page.evaluate(() => document.fonts.ready);
  await showCursor();
  mark("evidence");
  await wait(2600);
  await moveTo(1250, 620, 900);
  await scrollTo(page.locator("body"), { by: 420, ms: 3200 });
  mark("evidence-scrolled");
  await wait(2600);
});

await browser.close();
