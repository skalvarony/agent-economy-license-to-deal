import { test } from '@e2e-dev/web';
import { expect } from 'e2e';
import { createAccount, newCustomer } from './shop';

test('a person signs in, signs out and is refused with a wrong password', async ({ app, screen, browser }) => {
  const customer = newCustomer();
  await createAccount(customer);

  // There is no public registration: the shop sets the accounts up.
  expect((await fetch(`${process.env.APP_URL ?? 'http://localhost:8181'}/signup`)).status).toBe(404);
  await app.open('/login');
  await expect(screen.getByText(/Accounts are set up by the shop/)).toBeVisible();

  await screen.getByLabel('Email').fill(customer.email);
  await screen.getByLabel(/Password/).fill(customer.password);
  await screen.getByRole('button', 'Sign in').tap();
  await expect(browser.locator('.meta .who')).toHaveText('Test');

  await app.open('/account');
  await screen.getByRole('button', 'Sign out').tap();
  await expect(screen.getByRole('link', 'Sign in')).toBeVisible();

  // A wrong password is refused with the same message as an unknown email.
  await app.open('/login');
  await screen.getByLabel('Email').fill(customer.email);
  await screen.getByLabel(/Password/).fill('not the password');
  await screen.getByRole('button', 'Sign in').tap();
  await expect(screen.getByRole('status')).toHaveText("That email or password isn't right.");
});
