import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Playwright specs are not React. The React rules misread Playwright's
    // fixture callback argument — also named `use` — as the React `use` hook
    // and reject it for being called outside a component.
    "e2e/**",
    "playwright.config.ts",
  ]),
]);

export default eslintConfig;
