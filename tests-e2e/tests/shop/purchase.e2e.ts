import { test } from '@e2e-dev/web';
import { expect } from 'e2e';
import { createAccount, grantCoins, newCustomer, setBookingFee } from './shop';

async function signUp({ app, screen }, customer) {
  await createAccount(customer);
  await app.open('/login');
  await screen.getByLabel('Email').fill(customer.email);
  await screen.getByLabel(/Password/).fill(customer.password);
  await screen.getByRole('button', 'Sign in').tap();
}

async function addSpaDayToCart({ app, screen, browser }) {
  await app.open('/deals/spa_day_two');
  await screen.getByRole('radio', /3 hours/).check();
  await screen.getByRole('button', 'Add to cart').tap();
  // The cart opens beside the page, with the option that was chosen.
  await expect(browser.locator('#drawer h3')).toHaveText('Spa Day for Two · 3 hours');
  await browser.locator('#drawer form[action="/checkout"] button').tap();
}

// The spa day is booked for a date and time: the checkout page asks for
// them before it takes a payment.
async function chooseFirstSlot({ screen, browser }) {
  await expect(screen.getByRole('button', 'Choose a date and time first')).toBeVisible();
  // The first slot listed (the shops are freshly seeded, so it has room).
  await browser.locator('select[name="starts_at"]').selectOption({ index: 1 });
  await screen.getByRole('button', 'Set the time').tap();
  await expect(browser.locator('.slot .booked')).toContainText('Booked for');
}

test('a customer pays with coins and a card and gets a voucher code', async ({ app, screen, browser }) => {
  const customer = newCustomer();
  await grantCoins(customer.email, 40);
  await signUp({ app, screen }, customer);
  await expect(browser.locator('.meta .coins')).toHaveText('40 coins');

  await addSpaDayToCart({ app, screen, browser });
  await chooseFirstSlot({ screen, browser });
  await expect(screen.getByRole('button', 'Pay $99')).toBeVisible();
  await screen.getByRole('button', 'Use 40').tap();

  // The split is shown before paying: 40 coins and $59 on the card.
  await expect(browser.locator('.buy .sum')).toContainText('To pay by card');
  await screen.getByRole('button', 'Pay $59').tap();

  await expect(browser.locator('.notice.good')).toHaveText('Payment received. Your voucher is ready.');
  await expect(browser.locator('.code li strong')).toHaveText(/^DSK-[A-Z0-9]{4}-[A-Z0-9]{4}$/);
  // The voucher says when the visit is booked for.
  await expect(browser.locator('.lines .fine')).toContainText('Booked for');
  await expect(browser.locator('.buy .sum')).toContainText('40 coins');
  // 40 spent, 5 earned back on the $59 the card paid.
  await expect(browser.locator('.meta .coins')).toHaveText('5 coins');
});

test('a total that changes after the page opened is not charged without asking', async ({ app, screen, browser }) => {
  const customer = newCustomer();
  await signUp({ app, screen }, customer);
  await addSpaDayToCart({ app, screen, browser });
  await chooseFirstSlot({ screen, browser });
  await expect(screen.getByRole('button', 'Pay $99')).toBeVisible();

  await setBookingFee(2500);
  try {
    await screen.getByRole('button', 'Pay $99').tap();

    // Nothing was charged: the shop shows the old and the new total and asks.
    await expect(screen.getByRole('heading', 'The total changed')).toBeVisible();
    await expect(browser.locator('dialog#changed[open] .change')).toHaveText('$99$124');
    await expect(browser.locator('dialog#changed [data-breakdown]')).toContainText('Booking fee');
    await screen.getByRole('button', 'Accept and pay $124').tap();

    await expect(browser.locator('.notice.good')).toBeVisible();
    await expect(browser.locator('.buy .sum')).toContainText('$124');
  } finally {
    await setBookingFee(0);
  }
});
