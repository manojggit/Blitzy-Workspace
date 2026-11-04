// CardDemo end-to-end global setup and shared helpers (Technical Specification 0.9.2, 0.11.2).
//
// Replaces: the operator sequence that prepared the CICS region before a business day,
// CLOSEFIL, then the IDCAMS loader jobs ACCTFILE ... DUSRSECJ, then OPENFIL. Here it is one
// command, `carddemo-batch seed-all`, run once the backend answers its /health/ready probe.
// Sources: jcl/ACCTFILE.jcl, jcl/DUSRSECJ.jcl (loaders); bms/COSGN00.bms, cbl/COSGN00C.cbl
// (the sign-on screen the shared SignOn helper drives).
//
// Playwright runs the default export as globalSetup, and the function it returns as the global
// teardown that publishes failure evidence. Both specs import the named helpers, so importing
// this module performs no I/O: its top level only defines constants and functions.
// Every setting comes from frontend/.env.e2e, which playwright.config.ts loads; user IDs and
// passwords are read from the environment only and never appear in messages.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import { expect, type FullConfig, type Page } from "@playwright/test";

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

// A variable's value, or undefined when it is unset or blank. An empty process value counts as
// set, because process.loadEnvFile keeps it over the file, so it is treated as blank here.
function ReadEnv(name: RequiredVariable): string | undefined {
    const value = process.env[name];
    if (value === undefined || value.trim() === "") {
        return undefined;
    }
    return value;
}

// Names the missing variables, never a value: the values include passwords.
function DescribeMissingVariables(names: readonly RequiredVariable[]): string {
    const isSingle = names.length === 1;
    return (
        `Missing environment ${isSingle ? "variable" : "variables"} ${names.join(", ")}: copy ` +
        `frontend/.env.e2e.example to frontend/.env.e2e and set ${isSingle ? "it" : "them"}.`
    );
}

/**
 * The value of one .env.e2e variable. A missing or blank value fails with the variable's name
 * only: the values include passwords.
 */
export function RequireEnv(name: RequiredVariable): string {
    const value = ReadEnv(name);
    if (value === undefined) {
        throw new Error(DescribeMissingVariables([name]));
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
 * Asserts that the screen shows exactly one alert, whose text is exactly the legacy literal: a
 * prefixed, extended or second message fails. The raw textContent keeps the repeated and
 * trailing blanks that the whitespace-normalising text matchers would hide. Only alerts inside
 * the main landmark count, because the Next.js route announcer is an alert outside it.
 */
export async function ExpectMessage(page: Page, literal: string): Promise<void> {
    const screenAlerts = page.getByRole("main").getByRole("alert");
    await expect
        .poll(() => screenAlerts.allTextContents(), {
            message: `message line should show ${JSON.stringify(literal)}`,
            timeout: MESSAGE_TIMEOUT_MS,
        })
        .toEqual([literal]);
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

// CARDDEMO_E2E_BASE_URL and CARDDEMO_E2E_API_URL are origins: a scheme, a host and an optional
// port. Anything more is refused before any request is made, and no message repeats the value,
// which could carry a user name or password.
const ORIGIN_SCHEMES: readonly string[] = ["http:", "https:"];
const ORIGIN_PATHNAME = "/";
const ORIGIN_HINT = "set it to a scheme, host and port only, as in frontend/.env.e2e.example.";

// The TypeError from new URL holds the raw value in its input property, so it is replaced by a
// message naming the variable, never chained as a cause.
function ParseEnvUrl(name: RequiredVariable): URL {
    const value = RequireEnv(name);
    try {
        return new URL(value);
    } catch (error) {
        if (error instanceof TypeError) {
            throw new Error(`${name} is not a valid URL: ${ORIGIN_HINT}`);
        }
        throw error;
    }
}

function DescribeOriginProblem(url: URL): string | undefined {
    if (!ORIGIN_SCHEMES.includes(url.protocol)) {
        return "must use the http or https scheme";
    }
    if (url.username !== "" || url.password !== "") {
        return "must not carry a user name or password";
    }
    if (url.pathname !== ORIGIN_PATHNAME) {
        return "must not carry a path";
    }
    if (url.search !== "" || url.hash !== "") {
        return "must not carry a query or fragment";
    }
    return undefined;
}

function RequireOrigin(name: RequiredVariable): URL {
    const url = ParseEnvUrl(name);
    const problem = DescribeOriginProblem(url);
    if (problem !== undefined) {
        throw new Error(`${name} ${problem}: ${ORIGIN_HINT}`);
    }
    return url;
}

async function WaitForBackendReady(): Promise<void> {
    const readyUrl = new URL(READY_PATH, RequireOrigin("CARDDEMO_E2E_API_URL"));
    const deadline = Date.now() + READY_TIMEOUT_MS;
    while (Date.now() < deadline) {
        if (await IsBackendReady(readyUrl)) {
            return;
        }
        await Pause(READY_POLL_INTERVAL_MS);
    }
    const waitedSeconds = READY_TIMEOUT_MS / MILLISECONDS_PER_SECOND;
    throw new Error(
        `Backend not ready at ${readyUrl.origin}${READY_PATH} after ${waitedSeconds}` +
            " s: start PostgreSQL (docker compose up -d db), run alembic upgrade head, and start" +
            ` uvicorn on ${readyUrl.host} (docs/README.md).`,
    );
}

// Reports every missing or blank variable in one run, so one edit of .env.e2e fixes them all.
function ValidateEnvironment(): void {
    const missingNames = REQUIRED_VARIABLES.filter((name) => ReadEnv(name) === undefined);
    if (missingNames.length > 0) {
        throw new Error(DescribeMissingVariables(missingNames));
    }
}

// The API sets the SameSite=Lax session cookie, so the browser sends it on the page's API calls
// only while both origins share one host; ports do not count. The templates use 127.0.0.1 for
// both, because localhost is another site and may resolve to IPv6 first.
function ValidateSharedHost(): void {
    const pageHost = RequireOrigin("CARDDEMO_E2E_BASE_URL").hostname;
    const apiHost = RequireOrigin("CARDDEMO_E2E_API_URL").hostname;
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

/** Where one kind of failure evidence is published, by the extension Playwright gives it. */
interface EvidenceDestination {
    extension: string;
    directory: string;
}

// Playwright keeps each failed test's screenshot (test-failed-N.png) and video (video.webm) at
// the top of that test's own directory under the project's outputDir, and empties outputDir
// when the next run starts. The copies are kept in the repository's evidence folders. Nothing
// else is copied: error-context.md holds a page snapshot with the values typed into fields.
const EVIDENCE_DESTINATIONS: readonly EvidenceDestination[] = [
    { extension: ".png", directory: path.join(REPO_ROOT, "blitzy", "screenshots") },
    { extension: ".webm", directory: path.join(REPO_ROOT, "blitzy", "screen_recordings") },
];
const EVIDENCE_PREFIX = "e2e-";
const UNSAFE_FILE_NAME_CHARACTERS = /[^A-Za-z0-9._-]/g;

// Playwright names each test's directory uniquely within a run, from the spec file, the test
// title and the project, so the published name is stable across runs and a rerun of the same
// test overwrites its earlier copy instead of piling up new ones.
function PublishedName(testDirName: string, fileName: string): string {
    const publishedName = `${EVIDENCE_PREFIX}${testDirName}-${fileName}`;
    return publishedName.replace(UNSAFE_FILE_NAME_CHARACTERS, "-");
}

function PublishEvidenceFile(testDir: string, fileName: string): void {
    const extension = path.extname(fileName);
    const destination = EVIDENCE_DESTINATIONS.find((kind) => kind.extension === extension);
    if (destination === undefined) {
        return;
    }
    fs.mkdirSync(destination.directory, { recursive: true });
    const publishedName = PublishedName(path.basename(testDir), fileName);
    fs.copyFileSync(path.join(testDir, fileName), path.join(destination.directory, publishedName));
}

function PublishTestEvidence(testDir: string): void {
    for (const entry of fs.readdirSync(testDir, { withFileTypes: true })) {
        if (entry.isFile()) {
            PublishEvidenceFile(testDir, entry.name);
        }
    }
}

// An outputDir that no test has written to does not exist, and holds nothing to publish.
function PublishOutputDirEvidence(outputDir: string): void {
    if (!fs.existsSync(outputDir)) {
        return;
    }
    for (const entry of fs.readdirSync(outputDir, { withFileTypes: true })) {
        if (entry.isDirectory()) {
            PublishTestEvidence(path.join(outputDir, entry.name));
        }
    }
}

// Each project's resolved outputDir, which honours the --output command-line option.
function PublishFailureEvidence(config: FullConfig): void {
    const outputDirs = new Set(config.projects.map((project) => project.outputDir));
    for (const outputDir of outputDirs) {
        PublishOutputDirEvidence(outputDir);
    }
}

/**
 * Validates the settings, waits for the backend and seeds the sample data. The returned
 * function is Playwright's global teardown: once every test has finished, it copies each
 * failed test's screenshots to blitzy/screenshots and videos to blitzy/screen_recordings.
 */
export default async function GlobalSetup(config: FullConfig): Promise<() => void> {
    ValidateEnvironment();
    ValidateSharedHost();
    await WaitForBackendReady();
    SeedSampleData();
    return () => {
        PublishFailureEvidence(config);
    };
}
