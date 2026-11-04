// Playwright configuration for the CardDemo end-to-end flows (day in the life, role navigation).
//
// The suite runs against services that are already up (docs/README.md): PostgreSQL, the backend
// and the built frontend, so no webServer block starts anything here. Every setting, URLs and
// sign-on credentials included, comes from the uncommitted frontend/.env.e2e (a copy of
// .env.e2e.example); this file holds no URL, user ID or password and never prints a value.
import fs from "node:fs";
import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

/** The uncommitted e2e settings file, beside this config. */
const E2E_ENV_FILE = path.resolve(__dirname, ".env.e2e");

// The day-in-the-life flow runs `carddemo-batch run-all --start-at POSTTRAN` synchronously.
const TEST_TIMEOUT_MS = 180_000;
const EXPECT_TIMEOUT_MS = 10_000;

/**
 * Loads the e2e settings file when it exists. process.loadEnvFile keeps every variable already
 * set in the process environment, so those win over the file. A missing file is not an error
 * here: global setup then names each missing variable.
 */
function LoadE2eEnvironment(envFile: string): void {
    if (fs.existsSync(envFile)) {
        process.loadEnvFile(envFile);
    }
}

LoadE2eEnvironment(E2E_ENV_FILE);

export default defineConfig({
    testDir: "./e2e",
    testMatch: "**/*.spec.ts",
    globalSetup: "./e2e/global-setup.ts",
    // Both specs share one database, and the day-in-the-life flow adds a transaction and runs
    // the batch chain, so tests run one at a time.
    fullyParallel: false,
    workers: 1,
    forbidOnly: Boolean(process.env.CI),
    // The flow is stateful: a retry would start from an already-modified database.
    retries: 0,
    timeout: TEST_TIMEOUT_MS,
    expect: { timeout: EXPECT_TIMEOUT_MS },
    // No HTML report: it stores every step title, and Playwright titles a fill with the typed
    // value, so it would hold the sign-on passwords of every test, passing ones included.
    reporter: [["list"]],
    // Playwright's working files. Global setup's teardown copies each failed test's screenshot
    // and video from here to blitzy/screenshots and blitzy/screen_recordings.
    outputDir: "test-results",
    use: {
        // Must use host 127.0.0.1, like the API origin, or the SameSite=Lax session cookie is
        // not sent; global setup checks that both hosts match.
        baseURL: process.env.CARDDEMO_E2E_BASE_URL,
        // Tracing stays off: a trace records the sign-on request body and every fill value, so
        // it would store the passwords. The screenshot and video show the password field masked.
        trace: "off",
        screenshot: "only-on-failure",
        video: "retain-on-failure",
    },
    projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
