import { test as base, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

/**
 * A signed-in page, freshly authenticated for each test.
 *
 * Playwright's usual `storageState` trick does not work against this API, and
 * the reason is a deliberate security property rather than a bug: refresh
 * tokens rotate on every use, and presenting a consumed one is treated as
 * evidence of theft, which revokes the whole family. A saved state file is
 * therefore single-use — the first test spends it and every later test is
 * bounced to the login screen holding a revoked token.
 *
 * So each test gets its own session: log in through the API, then seed the
 * refresh token into localStorage before the app boots.
 */

const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8000/api/v1";
const REFRESH_KEY = "decisionflow.refresh";
const CREDENTIALS_FILE = "e2e/.auth/credentials.json";

export type Credentials = { email: string; password: string };

export function readCredentials(): Credentials {
  return JSON.parse(readFileSync(CREDENTIALS_FILE, "utf-8"));
}

async function login(credentials: Credentials): Promise<string> {
  const response = await fetch(`${API}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentials),
  });
  if (!response.ok) {
    throw new Error(`E2E login failed (${response.status}): ${await response.text()}`);
  }
  return (await response.json()).refresh_token;
}

export const test = base.extend<{ signedIn: Page }>({
  signedIn: async ({ page }, use) => {
    const refresh = await login(readCredentials());

    // Must run before any app code, so the auth provider sees the token on
    // its first render rather than after a redirect to /login.
    await page.addInitScript(
      ([key, value]) => window.localStorage.setItem(key, value),
      [REFRESH_KEY, refresh] as const,
    );

    await use(page);
  },
});

export { expect } from "@playwright/test";
