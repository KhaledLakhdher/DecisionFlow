import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";

/**
 * The Briefing tab: the agent fleet.
 *
 * A real briefing is several Gemini calls and around ten seconds, and the
 * endpoint is rate limited to ten per fifteen minutes per workspace. So one
 * run is performed and every assertion is made against it, rather than each
 * test triggering its own — which would exhaust the budget and make the suite
 * fail for a reason that has nothing to do with the code.
 *
 * The properties worth protecting here are honesty properties. The fleet must
 * not claim to have consulted a specialist that declined, and every figure in
 * the write-up must be traceable to a finding that computed it.
 */

async function openBriefing(page: Page) {
  await page.goto("/");
  await page.getByTestId("dataset-name").filter({ hasText: "orders" }).click();
  await expect(page.getByTestId("kpi-tile").first()).toBeVisible({ timeout: 60_000 });
  await page.getByRole("tab", { name: /briefing/i }).click();
  await expect(page.getByTestId("briefing-panel")).toBeVisible();
}

test.describe.serial("the fleet", () => {
  test("advertises which specialists this dataset can serve", async ({
    signedIn: page,
  }) => {
    await openBriefing(page);

    // Named up front so the cost of a run is predictable. `orders` has a date
    // column, a revenue measure and dimensions, so four specialists apply to a
    // blank run — and the panel must not imply the analyst is among them, since
    // it has nothing to query until something is asked.
    const roster = page.getByText(/A blank run consults/);
    await expect(roster).toBeVisible();
    await expect(roster).toContainText("Investigator, Sentinel, Forecaster, Retention");
    await expect(roster).toContainText(/Analyst once you ask something specific/);
  });

  test("does not run itself", async ({ signedIn: page }) => {
    await openBriefing(page);

    // Model calls cost money. Opening a tab must never spend any, so a
    // workspace that has never run one shows the button and nothing else.
    const summary = page.getByTestId("briefing-summary");
    if ((await summary.count()) === 0) {
      await expect(page.getByTestId("run-briefing")).toBeVisible();
    }
  });

  test("produces a brief backed by findings that computed it", async ({
    signedIn: page,
  }) => {
    test.setTimeout(180_000);
    await openBriefing(page);

    await page.getByTestId("run-briefing").click();
    await expect(page.getByTestId("briefing-summary")).toBeVisible({ timeout: 120_000 });

    // Every specialist that ran is listed with what it found.
    const findings = page.getByTestId("briefing-finding");
    expect(await findings.count()).toBeGreaterThan(1);

    // The investigator is the specialist that makes this more than a chatbot.
    await expect(page.getByText(/contribution analysis/i)).toBeVisible();

    await page.screenshot({ path: "e2e/.screenshots/13-briefing.png", fullPage: true });
  });

  test("shows a refusal rather than hiding it", async ({ signedIn: page }) => {
    await openBriefing(page);
    await expect(page.getByTestId("briefing-summary")).toBeVisible({ timeout: 120_000 });

    // Six customers is far below the churn threshold. The retention specialist
    // declines, and that has to reach the reader — a brief silently missing
    // retention implies it was looked at and found unremarkable.
    const refused = page.locator(
      '[data-testid="briefing-finding"][data-status="refused"]',
    );
    await expect(refused.first()).toBeVisible();
    await expect(refused.first()).toContainText(/at least 20 customers/i);
  });

  test("every finding carries its provenance", async ({ signedIn: page }) => {
    await openBriefing(page);
    await expect(page.getByTestId("briefing-summary")).toBeVisible({ timeout: 120_000 });

    // A generated number with no visible derivation is not checkable, which is
    // the whole objection to AI-written analysis.
    await expect(page.getByText(/^via /).first()).toBeVisible();

    const showQuery = page.getByRole("button", { name: /show the query/i }).first();
    await showQuery.click();
    await expect(page.locator("pre").first()).toContainText(/select/i);
  });

  test("separates what was computed from what was written", async ({
    signedIn: page,
  }) => {
    await openBriefing(page);
    await expect(page.getByTestId("briefing-summary")).toBeVisible({ timeout: 120_000 });

    // The distinction is stated on the page, not just implied by layout.
    await expect(
      page.getByText(/The write-up is\s+generated; these are computed\./i),
    ).toBeVisible();
  });
});
