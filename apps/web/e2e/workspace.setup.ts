import { expect, test as setup } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";

/**
 * One-time setup: register a workspace, upload a dataset, wait for the whole
 * pipeline to finish, then save the signed-in state for every other test.
 *
 * Registering per test seemed simpler and was wrong twice over. It runs the
 * ingestion pipeline once per test (slow, and each run is a chance to flake),
 * and it trips the API's own registration rate limit — 5 per hour per address,
 * which a seven-test suite blows straight through. The limiter is working
 * correctly; the test design was at fault.
 */

const PASSWORD = "correct-horse-battery-staple";
const CREDENTIALS_FILE = "e2e/.auth/credentials.json";

const CSV = `order_id,customer_id,Order Date,Revenue,Units,Unit Cost,Region,Category
1001,C-001,2026-01-05,"$1,200.00",3,600.00,  North  ,Electronics
1002,C-002,2026-01-20,$840.00,2,400.00,South,Furniture
1003,C-003,2026-02-05,"$2,150.00",6,980.00,North,Electronics
1004,C-001,2026-02-18,$430.00,1,250.00,East,Furniture
1005,C-004,2026-03-03,"$1,760.00",4,820.00,West,Electronics
1006,C-002,2026-03-22,N/A,2,300.00,South,Furniture
1007,C-005,2026-04-10,"$3,400.00",8,1500.00,North,Electronics
1007,C-005,2026-04-10,"$3,400.00",8,1500.00,North,Electronics
1009,C-006,2026-05-06,"$2,980.00",7,1290.00,West,Electronics
1010,C-001,2026-05-19,$710.00,2,340.00,North,Furniture
`;

setup("register a workspace and run one dataset through the pipeline", async ({ page }) => {
  setup.setTimeout(240_000);
  mkdirSync("e2e/.auth", { recursive: true });
  mkdirSync("e2e/.screenshots", { recursive: true });

  const email = `e2e-${Date.now()}@example.com`;

  await page.goto("/login");
  await page.getByRole("button", { name: /create a new workspace/i }).click();
  await page.getByLabel("Your name").fill("E2E Runner");
  await page.getByLabel("Workspace name").fill("Acme Retail");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: /create workspace/i }).click();

  await expect(page.getByRole("heading", { name: "Datasets" })).toBeVisible();
  await expect(page.getByText("No datasets yet")).toBeVisible();
  await page.screenshot({ path: "e2e/.screenshots/01-empty-state.png", fullPage: true });

  await page.setInputFiles('input[type="file"]', {
    name: "sales.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(CSV),
  });

  const row = page.getByTestId("dataset-row").first();
  await expect(row).toBeVisible();

  // The list polls; the worker flips this to "ready" when the pipeline is done.
  await expect(row).toHaveAttribute("data-status", "ready", { timeout: 180_000 });
  await page.screenshot({ path: "e2e/.screenshots/02-dataset-list.png", fullPage: true });

  // Credentials, not a saved session. Refresh tokens rotate and a replayed one
  // revokes the family, so a storageState file would be spent by the first
  // test that used it. Each test signs in for itself — see fixtures.ts.
  writeFileSync(CREDENTIALS_FILE, JSON.stringify({ email, password: PASSWORD }));
});
