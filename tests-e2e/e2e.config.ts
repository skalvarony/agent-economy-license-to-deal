import type { E2EConfig } from 'e2e';
import { web } from '@e2e-dev/web';

// The tests run against a shop that is already up (scripts/shops.sh start).
// APP_URL picks another shop: http://localhost:8182 or :8183.
// No model is configured: these tests use exact locators, not agent steps.
export default {
  targets: [{ engine: web(), app: { url: process.env.APP_URL ?? 'http://localhost:8181' } }],
} satisfies E2EConfig;
