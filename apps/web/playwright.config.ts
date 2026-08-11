import { defineConfig, devices } from "@playwright/test";

/**
 * E2E configuration.
 *
 * These tests drive the real stack — API, worker and web — rather than mocking
 * the backend. Mocked E2E tests pass while the product is broken, which defeats
 * the point of having them.
 *
 * The web server is started by Playwright; the API and worker must already be
 * running (see README). `reuseExistingServer` keeps local runs fast.
 */
export default defineConfig({
  testDir: "./e2e",
  // Screenshots are written on every run so the UI can be eyeballed without a
  // browser to hand — the palette validator checks color, never layout.
  outputDir: "./e2e/.artifacts",
  fullyParallel: false, // shared backend state
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],

  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },

  projects: [
    // Registers once, uploads once, waits for the pipeline, saves auth state.
    // Everything else depends on it, which keeps the suite inside the API's
    // registration rate limit and runs ingestion a single time.
    { name: "setup", testMatch: /.*\.setup\.ts/ },
    // No storageState here on purpose: refresh tokens rotate and a replayed
    // one revokes the family, so a shared session file is spent by the first
    // test that uses it. The `signedIn` fixture logs in per test instead.
    {
      name: "chromium",
      dependencies: ["setup"],
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  webServer: {
    command: "npm run dev",
    url: "http://localhost:3000",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
