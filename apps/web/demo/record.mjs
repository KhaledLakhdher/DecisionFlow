/**
 * Records the product demo.
 *
 * Playwright's video capture does not draw the mouse pointer, so an unassisted
 * run looks like things happening by themselves. A synthetic cursor and a
 * caption bar are injected to fix that — the captions also carry the
 * narration, since the video has no audio track.
 *
 * Every click goes through `click()` below, which moves the pointer to the
 * target and pauses before pressing. Without those pauses the demo is
 * technically correct and impossible to follow.
 *
 *   node demo/record.mjs
 */

import { chromium } from "@playwright/test";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const DATA = process.env.DEMO_DATA ?? join(HERE, "data");
const OUT = process.env.DEMO_OUT ?? join(HERE, "recording");
const WEB = process.env.DEMO_WEB ?? "http://localhost:3000";
const API = process.env.DEMO_API ?? "http://127.0.0.1:8001/api/v1";

const { email, password } = JSON.parse(
  readFileSync(join(DATA, "credentials.json"), "utf-8"),
);

const SIZE = { width: 1280, height: 720 };
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --------------------------------------------------------------------------
// Injected chrome: pointer, captions, and hiding the dev-server badge
// --------------------------------------------------------------------------
const OVERLAY = () => {
  const install = () => {
    if (document.getElementById("demo-cursor")) return;

    const style = document.createElement("style");
    style.textContent = [
      "nextjs-portal { display: none !important; }",
      "#demo-cursor { position: fixed; width: 22px; height: 22px; z-index: 2147483647;",
      "  pointer-events: none; left: -100px; top: -100px;",
      "  transition: left .28s cubic-bezier(.4,0,.2,1), top .28s cubic-bezier(.4,0,.2,1); }",
      "#demo-cursor svg { display: block; filter: drop-shadow(0 1px 3px rgba(0,0,0,.5)); }",
      "#demo-cursor.click::after { content: ''; position: absolute; left: -9px; top: -9px;",
      "  width: 40px; height: 40px; border-radius: 50%;",
      "  border: 2px solid rgba(37,99,235,.9); animation: demo-ping .5s ease-out; }",
      "@keyframes demo-ping { from { transform: scale(.3); opacity: 1; }",
      "  to { transform: scale(1); opacity: 0; } }",
      "#demo-caption { position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%);",
      "  z-index: 2147483646; pointer-events: none; background: rgba(17,17,20,.92); color: #fff;",
      "  font: 500 15px/1.45 ui-sans-serif, system-ui, sans-serif; padding: 11px 20px;",
      "  border-radius: 10px; max-width: 78%; text-align: center; opacity: 0;",
      "  transition: opacity .25s ease; box-shadow: 0 8px 28px rgba(0,0,0,.32); }",
      "#demo-caption.on { opacity: 1; }",
    ].join("\n");
    document.head.appendChild(style);

    const cursor = document.createElement("div");
    cursor.id = "demo-cursor";
    cursor.innerHTML =
      '<svg viewBox="0 0 24 24" width="22" height="22">' +
      '<path d="M5 2l7 18 2.5-7.5L22 10z" fill="#fff" stroke="#111" ' +
      'stroke-width="1.4" stroke-linejoin="round"/></svg>';
    document.body.appendChild(cursor);

    const caption = document.createElement("div");
    caption.id = "demo-caption";
    document.body.appendChild(caption);

    window.__demoMove = (x, y) => {
      cursor.style.left = x - 3 + "px";
      cursor.style.top = y - 2 + "px";
    };
    window.__demoClick = () => {
      cursor.classList.remove("click");
      void cursor.offsetWidth;
      cursor.classList.add("click");
    };
    window.__demoCaption = (text) => {
      if (!text) {
        caption.classList.remove("on");
        return;
      }
      caption.textContent = text;
      caption.classList.add("on");
    };
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install);
  } else {
    install();
  }
};

// --------------------------------------------------------------------------
// Driving helpers
// --------------------------------------------------------------------------
async function say(page, text, hold = 0) {
  await page.evaluate((value) => window.__demoCaption?.(value), text).catch(() => {});
  if (hold) await wait(hold);
}

async function moveTo(page, locator) {
  const box = await locator.boundingBox();
  if (!box) throw new Error("target has no bounding box");
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.evaluate(([px, py]) => window.__demoMove?.(px, py), [x, y]);
  await page.mouse.move(x, y);
  await wait(420);
}

async function click(page, locator) {
  await locator.scrollIntoViewIfNeeded().catch(() => {});
  await wait(200);
  await moveTo(page, locator);
  await page.evaluate(() => window.__demoClick?.());
  await wait(160);
  await locator.click();
  await wait(500);
}

async function type(page, locator, text) {
  await moveTo(page, locator);
  await page.evaluate(() => window.__demoClick?.());
  await locator.click();
  await locator.pressSequentially(text, { delay: 55 });
  await wait(350);
}

/** Scroll smoothly, so a long page reads as a pan rather than a jump. */
async function scrollTo(page, y, ms = 900) {
  await page.evaluate(
    ([target, duration]) =>
      new Promise((resolve) => {
        const start = window.scrollY;
        const delta = target - start;
        const began = performance.now();
        const step = (now) => {
          const progress = Math.min(1, (now - began) / duration);
          const eased =
            progress < 0.5
              ? 2 * progress * progress
              : 1 - Math.pow(-2 * progress + 2, 2) / 2;
          window.scrollTo(0, start + delta * eased);
          if (progress < 1) requestAnimationFrame(step);
          else resolve();
        };
        requestAnimationFrame(step);
      }),
    [y, ms],
  );
  await wait(250);
}

/** Clear datasets left by a previous run, so recordings are repeatable. */
async function resetWorkspace() {
  const login = await fetch(`${API}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!login.ok) {
    throw new Error(`demo login failed (${login.status}): ${await login.text()}`);
  }
  const token = (await login.json()).access_token;
  const headers = { Authorization: `Bearer ${token}` };

  const existing = await (await fetch(`${API}/datasets`, { headers })).json();
  for (const dataset of existing) {
    await fetch(`${API}/datasets/${dataset.id}`, { method: "DELETE", headers });
  }
  return existing.length;
}

// --------------------------------------------------------------------------
// The demo
// --------------------------------------------------------------------------
async function main() {
  mkdirSync(OUT, { recursive: true });

  const removed = await resetWorkspace();
  console.log(`workspace reset (${removed} dataset(s) removed)`);

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: SIZE,
    recordVideo: { dir: OUT, size: SIZE },
  });
  await context.addInitScript(OVERLAY);
  const page = await context.newPage();

  // ---- 1. sign in -------------------------------------------------------
  await page.goto(`${WEB}/login`);
  await page.waitForLoadState("networkidle");
  await wait(800);
  await say(page, "DecisionFlow — turn your data into decisions", 2400);
  await say(page, "A real multi-tenant SaaS. Every workspace is isolated at the database level.", 2200);

  await type(page, page.getByLabel(/email/i), email);
  await type(page, page.getByLabel(/password/i), password);
  await click(page, page.getByRole("button", { name: /^sign in$/i }));
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 30_000 });
  await wait(1000);

  // ---- 2. upload --------------------------------------------------------
  await say(page, "Start with nothing but the spreadsheets you already have.", 2000);

  for (const file of ["orders.csv", "customers.csv", "products.csv"]) {
    const button = page.getByRole("button", { name: /upload data/i });
    const [chooser] = await Promise.all([
      page.waitForEvent("filechooser"),
      click(page, button),
    ]);
    await chooser.setFiles(join(DATA, file));
    await say(page, `Uploading ${file}…`);
    await wait(1500);
  }

  await say(page, "Profiled, type-coerced, cleaned, and analysed — automatically.", 1800);
  await page
    .locator('[data-testid="dataset-row"][data-status="ready"]')
    .nth(2)
    .waitFor({ timeout: 180_000 });
  await say(page, "Three files ready. No configuration, no modelling by hand.", 2600);

  // ---- 3. the dashboard -------------------------------------------------
  await click(page, page.getByTestId("dataset-name").filter({ hasText: "orders" }));
  await page.getByTestId("kpi-tile").first().waitFor({ timeout: 90_000 });
  await wait(900);
  await say(page, "KPIs chosen from the data's shape — revenue, orders, customers, margin.", 2800);
  await scrollTo(page, 340);
  await say(page, "Every figure carries the SQL that produced it. Nothing is a black box.", 2800);
  await scrollTo(page, 720);
  await wait(1600);
  await scrollTo(page, 0);

  // ---- 4. quality -------------------------------------------------------
  await click(page, page.getByRole("tab", { name: /quality/i }));
  await wait(700);
  await say(page, "It reports what it changed: currency text parsed, a duplicate row removed.", 3200);
  await scrollTo(page, 320);
  await wait(2000);
  await scrollTo(page, 0);

  // ---- 5. the agent fleet ----------------------------------------------
  await click(page, page.getByRole("tab", { name: /briefing/i }));
  await wait(700);
  await say(page, "Now the part that makes it an analyst: a fleet of specialist agents.", 3000);
  await say(page, "An executive routes the work. The specialists compute the answers.", 2800);

  await click(page, page.getByTestId("run-briefing"));
  await say(page, "Four specialists, running in parallel against the real data…", 1200);
  await page.getByTestId("briefing-summary").waitFor({ timeout: 180_000 });
  await wait(1400);
  await say(page, "A written brief — where every figure was computed, not generated.", 3400);
  await scrollTo(page, 320);
  await say(page, "Then concrete recommendations, each tied to the evidence behind it.", 3200);
  await scrollTo(page, 700);
  await say(page, "The evidence itself: what each specialist found, and by what method.", 3200);
  await scrollTo(page, 1020);
  await wait(1800);

  const showQuery = page.getByRole("button", { name: /show the query/i }).first();
  if (await showQuery.count()) {
    await click(page, showQuery);
    await say(page, "Down to the exact query. A number you cannot check is a number you cannot use.", 3600);
  }
  await scrollTo(page, 0);

  // ---- 6. predictions ---------------------------------------------------
  await click(page, page.getByRole("tab", { name: /predictions/i }));
  await wait(800);
  await say(page, "Forecasting with real prediction intervals — and honest refusals when data is thin.", 3400);
  await scrollTo(page, 360);
  await wait(2000);
  await say(page, "Churn scored from RFM, with the definition shown so you can argue with it.", 3200);
  await scrollTo(page, 940);
  await wait(2400);
  await scrollTo(page, 0);

  // ---- 7. the data model -----------------------------------------------
  await click(page, page.getByRole("link", { name: /data model/i }));
  await page.getByRole("heading", { name: /data model/i }).waitFor();
  await wait(700);
  await say(page, "Uploaded files carry no foreign keys, so it infers them from the values.", 3200);

  await click(page, page.getByRole("button", { name: /detect relationships/i }));
  await page.getByTestId("proposed-relationship").first().waitFor({ timeout: 90_000 });
  await wait(1000);
  await say(page, "Proposed, never applied. A wrong join does not fail — it inflates every total.", 3600);
  await scrollTo(page, 440);
  await wait(1400);

  for (;;) {
    const remaining = page.getByTestId("proposed-relationship");
    if ((await remaining.count()) === 0) break;
    await click(page, remaining.first().getByRole("button", { name: /^confirm$/i }));
    await wait(800);
  }

  await scrollTo(page, 0);
  await page.getByTestId("star-diagram").waitFor({ timeout: 30_000 });
  await wait(700);
  await say(page, "Confirmed — and a star schema is built from your spreadsheets.", 3400);
  await scrollTo(page, 280);
  await wait(2600);

  // ---- 8. close ---------------------------------------------------------
  await scrollTo(page, 0);
  await say(page, "From spreadsheet to decision. DecisionFlow.", 3600);
  await say(page, "", 700);
  await wait(700);

  await context.close();
  await browser.close();
  console.log(`video written to ${OUT}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
