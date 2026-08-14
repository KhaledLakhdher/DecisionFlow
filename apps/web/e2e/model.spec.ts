import { expect, test } from "./fixtures";

/**
 * The data model page.
 *
 * The behaviour that matters most here is that nothing joins itself: detection
 * proposes, and a person confirms. A test that only checked "relationships
 * appear" would pass even if the app silently applied them.
 */

test("detection proposes relationships without applying them", async ({
  signedIn: page,
}) => {
  await page.goto("/model");
  await expect(page.getByRole("heading", { name: "Data model" })).toBeVisible();

  await page.getByRole("button", { name: /detect relationships/i }).click();

  const proposals = page.getByTestId("proposed-relationship");
  await expect(proposals.first()).toBeVisible({ timeout: 30_000 });

  // Proposed, not applied: no star schema exists yet.
  await expect(page.getByTestId("star-diagram-empty")).toBeVisible();

  await page.screenshot({ path: "e2e/.screenshots/11-model-proposed.png", fullPage: true });
});

test("every proposal shows its evidence", async ({ signedIn: page }) => {
  await page.goto("/model");
  await page.getByRole("button", { name: /detect relationships/i }).click();

  const first = page.getByTestId("proposed-relationship").first();
  await expect(first).toBeVisible({ timeout: 30_000 });

  // A confidence with no explanation is a number to be taken on faith.
  await expect(first).toContainText("%");
  await expect(first).toContainText(/values exist in/i);
});

test("confirming builds the star schema", async ({ signedIn: page }) => {
  await page.goto("/model");
  await page.getByRole("button", { name: /detect relationships/i }).click();
  await expect(page.getByTestId("proposed-relationship").first()).toBeVisible({
    timeout: 30_000,
  });

  // Confirm every proposal; each one rebuilds the model.
  for (;;) {
    const remaining = page.getByTestId("proposed-relationship");
    if ((await remaining.count()) === 0) break;
    await remaining.first().getByRole("button", { name: /confirm/i }).click();
    await page.waitForTimeout(400);
  }

  await expect(page.getByTestId("star-diagram")).toBeVisible();
  // Fact at the centre, dimensions around it.
  await expect(page.getByText(/star\.orders/)).toBeVisible();
  await expect(page.getByText("customers", { exact: true }).first()).toBeVisible();

  await page.screenshot({ path: "e2e/.screenshots/12-model-star.png", fullPage: true });
});

test("tables are labelled fact or dimension after confirmation", async ({
  signedIn: page,
}) => {
  await page.goto("/model");

  // Roles come from the reference graph, so they only exist once edges are
  // confirmed. This run reuses the state left by the test above when it ran
  // first; otherwise it confirms for itself.
  const proposals = page.getByTestId("proposed-relationship");
  if ((await proposals.count()) === 0) {
    await page.getByRole("button", { name: /detect relationships/i }).click();
    await page.waitForTimeout(1000);
  }
  for (;;) {
    const remaining = page.getByTestId("proposed-relationship");
    if ((await remaining.count()) === 0) break;
    await remaining.first().getByRole("button", { name: /confirm/i }).click();
    await page.waitForTimeout(400);
  }

  await expect(page.getByText(/fact ·/).first()).toBeVisible();
  await expect(page.getByText(/dimension ·/).first()).toBeVisible();
});

test("the model page is reachable from the header", async ({ signedIn: page }) => {
  await page.goto("/");
  await page.getByRole("link", { name: "Data model" }).click();
  await expect(page).toHaveURL(/\/model/);
});
