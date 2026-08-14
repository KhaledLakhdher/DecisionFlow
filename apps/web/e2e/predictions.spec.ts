import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";

/** The Predictions tab: forecast, anomalies, churn. */

async function openPredictions(page: Page) {
  await page.goto("/");
  await page.getByTestId("dataset-name").filter({ hasText: "orders" }).click();
  await expect(page.getByTestId("kpi-tile").first()).toBeVisible({ timeout: 60_000 });
  await page.getByRole("tab", { name: /predictions/i }).click();
}

test("the forecast chart plots history and a projection", async ({ signedIn: page }) => {
  await openPredictions(page);

  const history = page.getByTestId("history-series");
  const projection = page.getByTestId("forecast-series");

  await expect(history).toBeVisible();
  await expect(projection).toBeVisible();

  // Both must be real paths, not empty elements that still pass a visibility
  // check.
  expect(await history.getAttribute("d")).toMatch(/^M [\d.]+ [\d.]+ L/);
  expect(await projection.getAttribute("d")).toMatch(/^M [\d.]+ [\d.]+ L/);

  // The projection is dashed as well as differently coloured, so the two
  // halves stay distinguishable in greyscale and for a colorblind reader.
  expect(await projection.getAttribute("stroke-dasharray")).toBeTruthy();

  await page.screenshot({ path: "e2e/.screenshots/10-forecast.png", fullPage: true });
});

test("the forecast carries a legend and an uncertainty band", async ({ signedIn: page }) => {
  await openPredictions(page);

  // Two series means identity can never rest on colour alone.
  await expect(page.getByText("Actual", { exact: true })).toBeVisible();
  await expect(page.getByText(/Forecast \(\d+% range\)/)).toBeVisible();

  // A bare projected line would imply a precision no forecast has.
  await expect(page.locator("svg polygon").first()).toBeVisible();
});

test("the forecast explains which method was used", async ({ signedIn: page }) => {
  await openPredictions(page);

  // Five months of history is too short for seasonality, and the chart should
  // say so rather than leaving the reader to assume otherwise.
  await expect(page.getByText(/linear trend|Holt-Winters/i)).toBeVisible();
});

test("churn refuses honestly on too few customers", async ({ signedIn: page }) => {
  await openPredictions(page);

  // Six customers is well under the modelling threshold. The refusal is the
  // correct answer, and it must be shown rather than hidden.
  await expect(page.getByRole("heading", { name: "Customer retention" })).toBeVisible();
  await expect(page.getByText(/at least 20 customers/i)).toBeVisible();
});

test("the predictions tab is never silently empty", async ({ signedIn: page }) => {
  await openPredictions(page);

  const panel = page.getByRole("tabpanel");
  await expect(panel).not.toBeEmpty();
});
