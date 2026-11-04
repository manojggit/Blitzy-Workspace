// CardDemo end-to-end global setup and shared helpers (Technical Specification 0.9.2, 0.11.2).
//
// Replaces: the operator sequence that prepared the CICS region before a business day,
// CLOSEFIL, then the IDCAMS loader jobs ACCTFILE ... DUSRSECJ, then OPENFIL. Here it is one
// command, `carddemo-batch seed-all`, run once the backend answers its /health/ready probe.
// Sources: jcl/ACCTFILE.jcl, jcl/DUSRSECJ.jcl (loaders); bms/COSGN00.bms, cbl/COSGN00C.cbl
// (the sign-on screen the shared SignOn helper drives).
//
// Playwright runs the default export as globalSetup. Both specs import the named helpers, so
// importing this module performs no I/O: its top level only defines constants and functions.
// Every setting comes from frontend/.env.e2e, which playwright.config.ts loads; user IDs and
// passwords are read from the environment only and never appear in messages.
import { spawnSync } from "node:child_process";
import path from "node:path";

import { expect, type Page } from "@playwright/test";

/** The repository root: frontend/e2e -> frontend -> root. Relative settings resolve here. */
export const REPO_ROOT = path.resolve(__dirname, "..", "..");

/** The committed reconciliation fixtures the specs compare screen and batch output with. */
export const FIXTURE_DIR = path.join(REPO_ROOT, "batch", "tests", "fixtures", "expected");

/** Every variable of frontend/.env.e2e.example, in template order. */
export const REQUIRED_VARIABLES = [
    "CARDDEMO_E2E_BASE_URL",
    "CARDDEMO_E2E_API_URL",
    "CARDDEMO_E2E_USER_ID",
    "CARDDEMO_E2E_PASSWORD",
    "CARDDEMO_E2E_ADMIN_ID",
    "CARDDEMO_E2E_ADMIN_PASSWORD",
    "CARDDEMO_BATCH_BIN",
    "CARDDEMO_OUTPUT_DIR",
] as const;

type RequiredVariable = (typeof REQUIRED_VARIABLES)[number];

// The readiness probe is served outside /api/v1, so it resolves against the API origin.
const READY_PATH = "/health/ready";
const READY_STATUS = "ready";
const HTTP_OK = 200;
const READY_TIMEOUT_MS = 60_000;
const READY_POLL_INTERVAL_MS = 1_000;
const READY_REQUEST_TIMEOUT_MS = 2_000;
const MILLISECONDS_PER_SECOND = 1_000;

const BATCH_TIMEOUT_MS = 300_000;
const BATCH_MAX_BUFFER_BYTES = 16 * 1024 * 1024;
const EXIT_SUCCESS = 0;

/** The sign-on route, COSGN00 (CC00). */
export const SIGN_ON_ROUTE = "/signon";

/** How long a screen may take to show a server message. */
export const MESSAGE_TIMEOUT_MS = 15_000;

// The COSGN00 USERID and PASSWD labels and the ENTER=Sign-on legend. The button pattern matches
// "Sign on", "Sign-on" and "Signon", never "Sign out".
const USER_ID_LABEL = /User ID/i;
const PASSWORD_LABEL = /Password/i;
const SIGN_ON_BUTTON_NAME = /sign.?on/i;

/** A sign-on user ID and password, read from the environment. */
export interface Credentials {
    userId: string;
    password: string;
}

/** The outcome of one carddemo-batch command: its exit status and captured output. */
export interface BatchRun {
    status: number | null;
    stdout: string;
    stderr: string;
}

/**
 * The value of one .env.e2e variable. A missing or blank value fails with the variable's name
 * only: the values include passwords.
 */
export function RequireEnv(name: RequiredVariable): string {
    const value = process.env[name];
    if (value === undefined || value.trim() === "") {
        throw new Error(
            `Missing environment variable ${name}: copy frontend/.env.e2e.example to ` +
                "frontend/.env.e2e and set it.",
        );
    }
    return value;
}

/** An absolute path unchanged; a relative one against the repository root, never the cwd. */
export function ResolveRepoPath(value: string): string {
    if (path.isAbsolute(value)) {
        return value;
    }
    return path.resolve(REPO_ROOT, value);
}

/** The absolute batch output root: the GDG archives and statements/STATEMNT.PS. */
export function OutputDir(): string {
    return ResolveRepoPath(RequireEnv("CARDDEMO_OUTPUT_DIR"));
}

/** The regular user (any type but A), whom sign-on sends to the main menu. */
export function RegularUser(): Credentials {
    return {
        userId: RequireEnv("CARDDEMO_E2E_USER_ID"),
        password: RequireEnv("CARDDEMO_E2E_PASSWORD"),
    };
}

/** The admin user (type A), whom sign-on sends to the admin menu. */
export function AdminUser(): Credentials {
    return {
        userId: RequireEnv("CARDDEMO_E2E_ADMIN_ID"),
        password: RequireEnv("CARDDEMO_E2E_ADMIN_PASSWORD"),
    };
}

/**
 * Runs carddemo-batch from the repository root with an argument array, never a shell string,
 * and the output root passed as an absolute path. The exit status is returned, not judged:
 * callers assert the status their step expects.
 */
export function RunBatchCli(args: readonly string[]): BatchRun {
    const result = spawnSync(ResolveRepoPath(RequireEnv("CARDDEMO_BATCH_BIN")), [...args], {
        cwd: REPO_ROOT,
        env: { ...process.env, CARDDEMO_OUTPUT_DIR: OutputDir() },
        encoding: "utf8",
        shell: false,
        timeout: BATCH_TIMEOUT_MS,
        maxBuffer: BATCH_MAX_BUFFER_BYTES,
    });
    if (result.error !== undefined) {
        throw new Error(`carddemo-batch ${args.join(" ")} could not run: ${result.error.message}`);
    }
    return { status: result.status, stdout: result.stdout, stderr: result.stderr };
}

/** A failure message naming the command, its exit status and its labelled output. */
export function DescribeBatchRun(args: readonly string[], run: BatchRun): string {
    const exitStatus = run.status === null ? "without a status (killed by a signal)" : run.status;
    return [
        `carddemo-batch ${args.join(" ")} exited ${exitStatus}`,
        "--- stdout ---",
        run.stdout.trimEnd(),
        "--- stderr ---",
        run.stderr.trimEnd(),
    ].join("\n");
}

/** Waits for an exact pathname: /cards never matches /cards/view, and the query is ignored. */
export async function WaitForPath(page: Page, pathname: string): Promise<void> {
    await page.waitForURL((url) => url.pathname === pathname);
}

/**
 * Signs on through the COSGN00 screen. Typing waits until the fresh screen has put the cursor
 * on USERID, as the map's IC attribute did: the page sets that focus once it is interactive, so
 * no keystroke or click reaches a page that has not hydrated yet. The landing route is the
 * caller's to assert: COSGN00C sends type A to the admin menu and every other type to the main
 * menu.
 */
export async function SignOn(page: Page, credentials: Credentials): Promise<void> {
    await page.goto(SIGN_ON_ROUTE);
    const userIdField = page.getByLabel(USER_ID_LABEL);
    await expect(userIdField).toBeFocused({ timeout: MESSAGE_TIMEOUT_MS });
    await userIdField.fill(credentials.userId);
    await page.getByLabel(PASSWORD_LABEL).fill(credentials.password);
    await page.getByRole("button", { name: SIGN_ON_BUTTON_NAME }).click();
}

/**
 * Asserts that the message line shows the exact legacy literal. The raw textContent keeps the
 * repeated and trailing blanks that the whitespace-normalising text matchers would hide.
 */
export async function ExpectMessage(page: Page, literal: string): Promise<void> {
    await expect
        .poll(async () => (await page.getByRole("alert").allTextContents()).join("\n"), {
            message: `message line should show ${JSON.stringify(literal)}`,
            timeout: MESSAGE_TIMEOUT_MS,
        })
        .toContain(literal);
}

function Pause(milliseconds: number): Promise<void> {
    return new Promise((resolve) => {
        setTimeout(resolve, milliseconds);
    });
}

// The failures that mean "not ready yet": a refused connection (undici's TypeError "fetch
// failed"), a body that is not JSON, and the per-request timeout of AbortSignal.timeout.
function IsTransientFetchError(error: unknown): boolean {
    if (error instanceof TypeError || error instanceof SyntaxError) {
        return true;
    }
    return (
        error instanceof DOMException &&
        (error.name === "TimeoutError" || error.name === "AbortError")
    );
}

async function IsBackendReady(readyUrl: URL): Promise<boolean> {
    try {
        const response = await fetch(readyUrl, {
            signal: AbortSignal.timeout(READY_REQUEST_TIMEOUT_MS),
        });
        if (response.status !== HTTP_OK) {
            await response.body?.cancel();
            return false;
        }
        const body = (await response.json()) as { status?: unknown };
        return body.status === READY_STATUS;
    } catch (error) {
        if (IsTransientFetchError(error)) {
            return false;
        }
        throw error;
    }
}

async function WaitForBackendReady(): Promise<void> {
    const readyUrl = new URL(READY_PATH, RequireEnv("CARDDEMO_E2E_API_URL"));
    const deadline = Date.now() + READY_TIMEOUT_MS;
    while (Date.now() < deadline) {
        if (await IsBackendReady(readyUrl)) {
            return;
        }
        await Pause(READY_POLL_INTERVAL_MS);
    }
    const waitedSeconds = READY_TIMEOUT_MS / MILLISECONDS_PER_SECOND;
    throw new Error(
        `Backend not ready at ${readyUrl.href} after ${waitedSeconds}` +
            " s: start PostgreSQL (docker compose up -d db), run alembic upgrade head, and start" +
            ` uvicorn on ${readyUrl.host} (docs/README.md).`,
    );
}

function ValidateEnvironment(): void {
    for (const name of REQUIRED_VARIABLES) {
        RequireEnv(name);
    }
}

// The API sets the SameSite=Lax session cookie, so the browser sends it on the page's API calls
// only while both origins share one host; ports do not count. The templates use 127.0.0.1 for
// both, because localhost is another site and may resolve to IPv6 first.
function ValidateSharedHost(): void {
    const pageHost = new URL(RequireEnv("CARDDEMO_E2E_BASE_URL")).hostname;
    const apiHost = new URL(RequireEnv("CARDDEMO_E2E_API_URL")).hostname;
    if (pageHost !== apiHost) {
        throw new Error(
            `CARDDEMO_E2E_BASE_URL (host ${pageHost}) and CARDDEMO_E2E_API_URL (host ${apiHost})` +
                " must use the same host, 127.0.0.1, or the session cookie is not sent.",
        );
    }
}

// Destructive to the database in backend/.env, normally the local carddemo database, as
// docs/README.md warns: seed-all abandons the open batch cycle and reloads every loader, users
// included. TRANFILE loads 0 rows, so transactions.transactions is left empty and the first
// online add of day-in-the-life.spec.ts receives TRAN-ID 0000000000000001.
function SeedSampleData(): void {
    const seedArgs = ["seed-all"] as const;
    const run = RunBatchCli(seedArgs);
    if (run.status !== EXIT_SUCCESS) {
        throw new Error(DescribeBatchRun(seedArgs, run));
    }
}

export default async function GlobalSetup(): Promise<void> {
    ValidateEnvironment();
    ValidateSharedHost();
    await WaitForBackendReady();
    SeedSampleData();
}
