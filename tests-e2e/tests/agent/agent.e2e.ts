// The customer's agent (scripts/agent.sh start), with the three shops up and
// requiring signatures (REQUIRE_SIGNATURES=1 scripts/shops.sh start).
// Run with `npm run test:agent`.
import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

const AGENT = process.env.APP_URL ?? 'http://localhost:8190';
const SHOP_B = process.env.SHOP_B_URL ?? 'http://localhost:8182';
const SECRET = process.env.SIMULATION_SECRET ?? 'demo-secret';

async function newConversation(app) {
  await fetch(`${AGENT}/api/reset`, { method: 'POST' });
  await app.open('/');
}

// The thread never resets, so a request is typed rather than picked from
// the suggestions an empty thread shows. The deals are booked for a date
// and time, so the request names them (Saturday: the spa day's date).
async function ask({ browser }, text) {
  await browser.locator('#say').fill(text);
  await browser.locator('#composer button').tap();
}

async function setBookingFee(cents: number) {
  const response = await fetch(`${SHOP_B}/testing/booking-fee`, {
    method: 'POST',
    headers: { 'Simulation-Secret': SECRET, 'Content-Type': 'application/json' },
    body: JSON.stringify({ amount: cents }),
  });
  if (!response.ok) throw new Error(`fee failed: ${response.status}`);
}

test('the agent compares the shops, proposes one deal and buys it once approved', async ({ app, screen, browser }) => {
  await newConversation(app);
  await ask({ browser }, 'A spa day for two, refundable, under $120, Saturday at 11:00');

  await expect(browser.locator('.card.proposal h3')).toHaveText('Spa Day for Two · 3 hours');
  // Booked for the slot that was asked for.
  await expect(browser.locator('.card.proposal').last()).toContainText('11:00');
  // The other shops were looked at and turned down, with the reason.
  await expect(browser.locator('.card.proposal .compared')).toContainText('Non-refundable');
  await expect(browser.locator('.card.proposal .compared')).toContainText('No wellness deals');
  // Every request to a shop is listed for this turn: three searches, three
  // wallets, the spa's availability, one checkout.
  await expect(browser.locator('.steps summary').last()).toHaveText('8 signed requests to 3 shops');
  await expect(browser.locator('.receipt')).toHaveCount(0);

  await screen.getByRole('button', /^Approve \$/).tap();

  await expect(browser.locator('.receipt .codes li')).toHaveText(/^DSK-[A-Z0-9]{4}-[A-Z0-9]{4}$/);
  await expect(browser.locator('.receipt .match')).toContainText('Charged exactly what you approved');
  await expect(browser.locator('.card.proposal .state').last()).toHaveText('Bought, at the total you approved.');

  // The evidence behind the order: the agent's record and the shop's order agree.
  await screen.getByRole('link', 'the evidence').tap();
  await expect(browser.locator('.findings li.yes')).toHaveCount(5);
  await expect(browser.locator('.findings li.no')).toHaveCount(0);
  await expect(browser.locator('.facts')).toContainText('Verified (key shopping-agent)');
  await expect(browser.locator('.timeline li')).toHaveCount(2);
});

test('a total the shop changes after the approval is not paid without asking again', async ({ app, screen, browser }) => {
  await newConversation(app);
  await ask({ browser }, 'A beer tasting for two under $50, Saturday at 17:00');
  // This turn's proposal, open, before the fee is switched on.
  await expect(browser.locator('.card.proposal').last()).toContainText('Beer Tasting');
  await expect(screen.getByRole('button', /^Approve \$/)).toBeVisible();

  await setBookingFee(1500);
  try {
    await screen.getByRole('button', /^Approve \$/).tap();

    await expect(browser.locator('.card.proposal > .changed').last()).toContainText('Nothing was paid');
    await expect(browser.locator('.card.proposal').last()).toContainText('Booking fee');

    // The new total needs its own approval.
    await screen.getByRole('button', /^Approve \$/).tap();
    await expect(browser.locator('.receipt .codes li').last()).toHaveText(/^PRP-/);
  } finally {
    await setBookingFee(0);
  }
});

test('a proposal the person turns down is not bought', async ({ app, screen, browser }) => {
  await newConversation(app);
  await ask({ browser }, 'A beer tasting for two under $50, Saturday at 17:00');
  await screen.getByRole('button', 'Not this one').tap();

  await expect(browser.locator('.card.proposal .state').last()).toHaveText('You turned this down. Nothing was paid.');
  await expect(screen.getByRole('button', /^Approve \$/)).toHaveCount(0);
});
