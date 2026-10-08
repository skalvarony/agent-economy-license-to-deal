import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

test('search narrows the deals as you type', async ({ app, screen, browser }) => {
  await app.open('/');
  await expect(browser.locator('#deals .card:not([hidden])')).toHaveCount(4);

  await screen.getByLabel('Search deals').fill('sauna');

  await expect(browser.locator('#deals .card:not([hidden])')).toHaveCount(1);
  await expect(screen.getByRole('heading', 'Private Sauna Evening for Two')).toBeVisible();
});

test('each option of a deal has its own price', async ({ app, screen, browser }) => {
  await app.open('/deals/spa_day_two');
  const price = browser.locator('[data-option-price]');
  // The cheapest option on sale is picked to start with.
  await expect(price).toContainText('$69');

  await screen.getByRole('radio', /Full day/).check();

  await expect(price).toContainText('$139');
  await expect(browser.locator('[data-option-save]')).toHaveText('You save 47%');
  await expect(browser.locator('[data-includes]:not([hidden])')).toContainText('Lunch for two');
});
