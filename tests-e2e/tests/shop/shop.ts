// What the tests share: the shop's address, its merchant secret, and helpers
// that prepare data through the shop's own API.
import { randomBytes } from 'node:crypto';

export const SHOP = process.env.APP_URL ?? 'http://localhost:8181';
const SECRET = process.env.SIMULATION_SECRET ?? 'demo-secret';
const MERCHANT = { 'Simulation-Secret': SECRET, 'Content-Type': 'application/json' };

/** A customer nobody has used before, so a test never depends on another. */
export function newCustomer() {
  const id = randomBytes(4).toString('hex');
  return {
    name: 'Test Buyer',
    email: `buyer-${id}@example.com`,
    // Made up for this run; it only exists in the shop's test database.
    password: randomBytes(12).toString('base64url'),
  };
}

/** Create a customer's account, as the shop does; people can't register. */
export async function createAccount(customer: { name: string; email: string; password: string }) {
  const response = await fetch(`${SHOP}/accounts`, {
    method: 'POST',
    headers: MERCHANT,
    body: JSON.stringify({ full_name: customer.name, email: customer.email, password: customer.password }),
  });
  if (!response.ok) throw new Error(`account failed: ${response.status}`);
}

/** Give a customer coins, as the shop would. */
export async function grantCoins(email: string, coins: number) {
  const response = await fetch(`${SHOP}/wallets/grant`, {
    method: 'POST',
    headers: MERCHANT,
    body: JSON.stringify({ email, coins }),
  });
  if (!response.ok) throw new Error(`grant failed: ${response.status}`);
}

/** Add a booking fee to every checkout, or remove it with 0. */
export async function setBookingFee(cents: number) {
  const response = await fetch(`${SHOP}/testing/booking-fee`, {
    method: 'POST',
    headers: MERCHANT,
    body: JSON.stringify({ amount: cents }),
  });
  if (!response.ok) throw new Error(`fee failed: ${response.status}`);
}
