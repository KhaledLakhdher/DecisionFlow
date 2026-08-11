import { expect, test } from "@playwright/test";

/**
 * Signed-out behaviour. These use the plain `page`, which carries no session.
 */

test("an unauthenticated visitor is sent to the login page", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login/);
});

test("bad credentials show the API's own message", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill("nobody@example.com");
  await page.getByLabel("Password").fill("wrong-password-here");
  await page.getByRole("button", { name: /sign in/i }).click();

  // Scoped to alerts with actual content: Next.js renders an empty
  // role="alert" route announcer on every page, which an unscoped
  // getByRole("alert") also matches.
  const alert = page.getByRole("alert").filter({ hasText: /\S/ });
  await expect(alert).toContainText(/incorrect|invalid|too many/i);

  await page.screenshot({ path: "e2e/.screenshots/08-login-error.png" });
});

test("the login form is reachable and labelled", async ({ page }) => {
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "DecisionFlow" })).toBeVisible();
  await expect(page.getByLabel("Email")).toBeVisible();
  await expect(page.getByLabel("Password")).toBeVisible();
});
