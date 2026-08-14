import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";

/**
 * Dashboard behaviour, against the real stack.
 *
 * The workspace and its dataset come from workspace.setup.ts; the `signedIn`
 * fixture gives each test its own session. These tests neither register nor
 * wait for ingestion — they assert what the UI does with data already there.
 */

/**
 * Waits on the KPI tile itself, by test id.
 *
 * `getByText("Total revenue")` looks right and is not: text matching is
 * case-insensitive substring, so it also matched the chat suggestion button
 * "What is our total revenue?". The wait returned as soon as the chat panel
 * rendered, long before any KPI existed — which made every screenshot below
 * capture a half-loaded page.
 */
async function openDashboard(page: Page) {
  await page.goto("/");
  // The fact table — the one with KPIs, a forecast and dimensions to join.
  await page.getByTestId("dataset-name").filter({ hasText: "orders" }).click();
  await expect(page.getByTestId("kpi-tile").first()).toBeVisible({ timeout: 60_000 });
}

async function openTab(page: Page, name: string) {
  await page.getByRole("tab", { name: new RegExp(name, "i") }).click();
}

test("the dashboard shows generated KPIs", async ({ signedIn: page }) => {
  await openDashboard(page);

  await expect(page.locator('[data-kpi="total_revenue"]')).toBeVisible();
  await expect(page.locator('[data-kpi="average_order_value"]')).toBeVisible();
  await expect(page.locator('[data-kpi="unique_customers"]')).toBeVisible();

  // The headline figure leads the grid; it is ordered by importance, not key.
  await expect(page.getByTestId("kpi-tile").first()).toHaveAttribute(
    "data-kpi",
    "total_revenue",
  );

  await page.screenshot({ path: "e2e/.screenshots/03-dashboard.png", fullPage: true });
});

test("cleaning removed the duplicate row", async ({ signedIn: page }) => {
  // 10 rows uploaded, one an exact duplicate. "9 rows" also appears in the
  // table's own "First 9 of 9 rows." caption, so this anchors on the summary
  // line under the dataset heading.
  await openDashboard(page);
  await expect(page.getByText(/9 rows · 8 columns/)).toBeVisible();
});

test("the line chart plots a real path", async ({ signedIn: page }) => {
  await openDashboard(page);

  // Targeted by test id, not `svg path[stroke]` — that also matches Next's
  // dev-indicator icon, so the assertion passed on the wrong element.
  // A chart with no path is a blank card that still clears a visibility check.
  const line = page.getByTestId("line-series");
  await expect(line).toBeVisible();
  expect(await line.getAttribute("d"), "line chart must plot a path").toMatch(/^M [\d.]+ [\d.]+ L/);
});

test("the breakdown chart renders bars", async ({ signedIn: page }) => {
  await openDashboard(page);
  await expect(page.getByText(/by region|by category/i)).toBeVisible();
});

test("KPI tiles expose the SQL behind each number", async ({ signedIn: page }) => {
  await openDashboard(page);

  await page.getByRole("button", { name: /how is this calculated/i }).first().click();
  await expect(page.locator("pre").first()).toContainText(/SELECT/i);

  await page.screenshot({ path: "e2e/.screenshots/04-kpi-sql.png", fullPage: true });
});

test("the quality tab explains what was cleaned", async ({ signedIn: page }) => {
  await openDashboard(page);
  await openTab(page, "Quality");

  await expect(page.getByRole("heading", { name: "Data quality" })).toBeVisible();
  await expect(page.getByText("What was cleaned")).toBeVisible();

  // "duplicate" appears twice by design — once as a finding, once as the
  // cleaning action taken — so both are asserted rather than matched loosely.
  await expect(page.getByText(/1 duplicate row \(/i)).toBeVisible();
  await expect(page.getByText(/Exact duplicate rows were removed/i)).toBeVisible();

  await page.screenshot({ path: "e2e/.screenshots/05-quality.png", fullPage: true });
});

test("tab labels advertise what is behind them", async ({ signedIn: page }) => {
  // The whole cost of a tab is that it hides content; the count is the signal
  // that makes it worth clicking.
  await openDashboard(page);

  const predictions = page.getByRole("tab", { name: /predictions/i });
  const quality = page.getByRole("tab", { name: /quality/i });

  await expect(predictions).toContainText(/\d/);
  await expect(quality).toContainText(/\d/);
});

test("tabs are keyboard navigable", async ({ signedIn: page }) => {
  await openDashboard(page);

  await page.getByRole("tab", { name: /overview/i }).focus();
  await page.keyboard.press("ArrowRight");

  await expect(page.getByRole("tab", { name: /predictions/i })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

test("the data tab shows the schema and cleaned rows", async ({ signedIn: page }) => {
  await openDashboard(page);
  await openTab(page, "Data");

  // Role + name, not getByText: text matching is case-insensitive substring,
  // so "Cleaned data" also matches the chat blurb "...querying your cleaned
  // data...". This bit three separate assertions in this file.
  await expect(page.getByRole("heading", { name: "Columns" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Cleaned data" })).toBeVisible();
  // Currency text became a number, so the raw "$" form must be gone.
  await expect(page.getByRole("table").last()).not.toContainText("$1,200.00");

  await page.screenshot({ path: "e2e/.screenshots/09-data-tab.png", fullPage: true });
});

test("dark mode uses its own selected palette", async ({ signedIn: page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await openDashboard(page);

  // The dark surface is a chosen step, not an inverted light one.
  const background = await page.evaluate(
    () => getComputedStyle(document.body).backgroundColor,
  );
  expect(background).toBe("rgb(13, 13, 13)");

  // Asserted immediately before the screenshot, not just on arrival, so a
  // half-rendered page cannot be captured as if it were the finished article.
  // Overview content only — the data table now lives behind the Data tab.
  await expect(page.locator('[data-kpi="total_revenue"]')).toBeVisible();
  await expect(page.getByTestId("history-series").or(page.locator("svg path[stroke]").first()))
    .toBeVisible();

  await page.screenshot({ path: "e2e/.screenshots/06-dark-mode.png", fullPage: true });
});

test("the page never scrolls horizontally", async ({ signedIn: page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openDashboard(page);

  // Wide tables must scroll inside their own container, never the body.
  const overflows = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  );
  expect(overflows, "body must not scroll horizontally").toBeFalsy();
});

test("the layout holds at a narrow viewport", async ({ signedIn: page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openDashboard(page);

  const overflows = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  );
  expect(overflows, "mobile layout must not overflow").toBeFalsy();

  await page.screenshot({ path: "e2e/.screenshots/07-mobile.png", fullPage: true });
});
