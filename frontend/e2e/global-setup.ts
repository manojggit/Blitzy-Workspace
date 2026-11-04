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
// The e2e variables are read from the process environment, which playwright.config.ts fills from
// frontend/.env.e2e without overriding a value already set; user IDs and passwords are read from
// the environment only and never appear in messages. APP_ENV and SYNC_DATABASE_URL are read as
// the batch CLI reads them, from the environment or else from plain unquoted backend/.env lines,
// and the URL as SQLAlchemy parses it. Every carddemo-batch command that global setup or a spec
// runs is given the checked values, and no message repeats them.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { parseEnv } from "node:util";

import { expect, type FullConfig, type Locator, type Page } from "@playwright/test";

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
 * The environment's value of one .env.e2e variable. A missing or blank value fails with the
 * variable's name only: the values include passwords.
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
 * and the output root passed as an absolute path. It first refuses a batch target that is not
 * local, and gives the command the checked APP_ENV and SYNC_DATABASE_URL. The exit status is
 * returned, not judged: callers assert the status their step expects.
 */
export function RunBatchCli(args: readonly string[]): BatchRun {
    const result = spawnSync(ResolveRepoPath(RequireEnv("CARDDEMO_BATCH_BIN")), [...args], {
        cwd: REPO_ROOT,
        env: BatchCliEnvironment(),
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

// Enters a credential with the steps locator.fill runs for a text or password input, minus the
// one that logs the value: fill(value) writes it into its call log, which a failed action's
// error message carries to the reporter and error-context.md. clear() waits for the field to be
// visible, enabled and editable, then selects, focuses and empties it, logging only fill("");
// insertText types into the focused field and logs nothing.
async function EnterCredential(field: Locator, value: string): Promise<void> {
    await field.clear();
    await expect(field).toBeFocused();
    await field.page().keyboard.insertText(value);
}

/**
 * Signs on through the COSGN00 screen. Typing waits until the fresh screen has put the cursor
 * on USERID, as the map's IC attribute did: the page sets that focus once it is interactive, so
 * no keystroke or click reaches a page that has not hydrated yet. Both credentials are entered
 * by clearing the field and inserting the text, so no failure message, call log or error cause
 * carries a user ID or password. The landing route is the caller's to assert: COSGN00C sends
 * type A to the admin menu and every other type to the main menu.
 */
export async function SignOn(page: Page, credentials: Credentials): Promise<void> {
    await page.goto(SIGN_ON_ROUTE);
    const userIdField = page.getByLabel(USER_ID_LABEL);
    await expect(userIdField).toBeFocused({ timeout: MESSAGE_TIMEOUT_MS });
    await EnterCredential(userIdField, credentials.userId);
    await EnterCredential(page.getByLabel(PASSWORD_LABEL), credentials.password);
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

// The one host of every local URL: localhost is another site and may resolve to IPv6 first.
const LOCAL_HOSTNAME = "127.0.0.1";

/** A parsed origin and the variable it came from, which a refusal names. */
interface NamedOrigin {
    name: RequiredVariable;
    url: URL;
}

function RequireNamedOrigin(name: RequiredVariable): NamedOrigin {
    return { name, url: RequireOrigin(name) };
}

// Names every origin whose host is not 127.0.0.1, with its parsed host only: RequireOrigin has
// already refused user names and passwords.
function DescribeNonLocalOrigins(origins: readonly NamedOrigin[]): string | undefined {
    const nonLocalOrigins = origins.filter((origin) => origin.url.hostname !== LOCAL_HOSTNAME);
    if (nonLocalOrigins.length === 0) {
        return undefined;
    }
    const hosts = nonLocalOrigins.map((origin) => `${origin.name} (host ${origin.url.hostname})`);
    return (
        `${hosts.join(" and ")} must use host ${LOCAL_HOSTNAME}, or the SameSite=Lax session` +
        " cookie is not sent."
    );
}

// The API sets the SameSite=Lax session cookie, which the browser sends on the page's API calls
// only when both origins are the same site: one host, 127.0.0.1, and one scheme, because
// SameSite is schemeful. Ports do not count, so each clone may use its own.
function ValidateLocalOrigins(): void {
    const pageOrigin = RequireNamedOrigin("CARDDEMO_E2E_BASE_URL");
    const apiOrigin = RequireNamedOrigin("CARDDEMO_E2E_API_URL");
    const hostProblem = DescribeNonLocalOrigins([pageOrigin, apiOrigin]);
    if (hostProblem !== undefined) {
        throw new Error(hostProblem);
    }
    if (pageOrigin.url.protocol !== apiOrigin.url.protocol) {
        throw new Error(
            `CARDDEMO_E2E_BASE_URL (scheme ${pageOrigin.url.protocol}) and` +
                ` CARDDEMO_E2E_API_URL (scheme ${apiOrigin.url.protocol}) must use the same` +
                " scheme, or the SameSite=Lax session cookie is not sent.",
        );
    }
}

// The two settings that decide which database a carddemo-batch command uses, resolved as
// batch/config.BatchSettings resolves them: a nonempty process value, then a nonempty value in
// backend/.env (app.core.paths.ENV_FILE), then the default, which only APP_ENV has.
type BatchVariable = "APP_ENV" | "SYNC_DATABASE_URL";

/** backend/.env as text and as parseEnv reads it; both empty when the file is absent. */
interface BatchEnvFile {
    text: string;
    values: NodeJS.Dict<string>;
}

/**
 * One batch CLI setting and the source it came from, undefined for both when unset.
 * isPlainLine is false when backend/.env assigns the setting in a form that python-dotenv, the
 * CLI's parser, may read differently from parseEnv.
 */
interface BatchSetting {
    name: BatchVariable;
    value: string | undefined;
    source: string | undefined;
    isPlainLine: boolean;
}

/** The checked APP_ENV and SYNC_DATABASE_URL that every carddemo-batch command is given. */
interface BatchTarget {
    appEnv: string;
    syncDatabaseUrl: string;
}

const BATCH_ENV_FILE = path.join(REPO_ROOT, "backend", ".env");
const BATCH_ENV_FILE_SOURCE = "backend/.env";
const PROCESS_ENV_SOURCE = "the environment";
// The CLI reads the environment alone when backend/.env is missing or is not a file.
const ABSENT_FILE_ERROR_CODES: readonly string[] = ["ENOENT", "ENOTDIR", "EISDIR"];
const DEFAULT_APP_ENV = "local";
const BATCH_DEFAULTS: Readonly<Record<BatchVariable, string | undefined>> = {
    APP_ENV: DEFAULT_APP_ENV,
    SYNC_DATABASE_URL: undefined,
};
const SEEDABLE_APP_ENVS: readonly string[] = ["local", "test"];
// The batch CLI parses SYNC_DATABASE_URL with SQLAlchemy's make_url, so this is the pattern of
// sqlalchemy.engine.url._parse_url in SQLAlchemy 2.0.36. Python's re.match anchors it at the
// start only, as "^" does here. Over printable ASCII, \w and "." match the same characters in
// both languages, so the URL is refused unless it is printable ASCII.
const PRINTABLE_ASCII_PATTERN = /^[\x21-\x7E]*$/;
const SQLALCHEMY_URL_PATTERN = new RegExp(
    [
        String.raw`^(?<name>[\w\+]+)://`,
        String.raw`(?:(?<username>[^:/]*)(?::(?<password>[^@]*))?@)?`,
        String.raw`(?:(?:\[(?<ipv6host>[^/\?]+)\]|(?<ipv4host>[^/:\?]+))?(?::(?<port>[^/\?]*))?)?`,
        String.raw`(?:/(?<database>[^\?]*))?`,
        String.raw`(?:\?(?<query>.*))?`,
    ].join(""),
);
// make_url converts a port that follows ":" with int(), and the CLI refuses the URL when that
// fails, as it does for an empty port; only ASCII digits are accepted here.
const PORT_PATTERN = /^[0-9]+$/;
// libpq connects to the server these query parameters name, whatever host the URL carries.
const HOST_OVERRIDE_PARAMETERS: readonly string[] = ["host", "hostaddr", "service"];
// libpq reads these from the CLI's environment whenever the URL omits hostaddr or service, and
// then connects to the server they give, whatever host the URL carries.
const LIBPQ_ADDRESS_VARIABLES: readonly string[] = ["PGHOSTADDR", "PGSERVICE"];
// python-dotenv expands ${NAME} in backend/.env values, so the value the CLI would use is unknown.
const INTERPOLATION_MARKER = "${";
const EDGE_BLANKS_PATTERN = /^[ \t]+|[ \t]+$/g;
// A value that opens with a quote, which parseEnv also reads as quoting when it is a backtick.
const QUOTE_START_PATTERN = /^["'`]/;
const UNSET_PROBLEM = `is set in neither ${PROCESS_ENV_SOURCE} nor ${BATCH_ENV_FILE_SOURCE}`;
const PLAIN_LINE_PROBLEM =
    "must be a plain unquoted value with no comment, so global setup reads it as the batch CLI" +
    " does";
const BATCH_TARGET_HINT =
    "carddemo-batch, whose seed-all resets its database, runs only with APP_ENV local or test and" +
    ` a SYNC_DATABASE_URL whose host, as SQLAlchemy parses it, is ${LOCAL_HOSTNAME}. Both are` +
    " read as the CLI reads them, from the environment or else from plain unquoted backend/.env" +
    " lines, and every command is given the values checked. carddemo-batch was not started.";

// Node's file-system errors name their errno in code; any other failure propagates.
function IsAbsentFileError(error: unknown): boolean {
    if (typeof error !== "object" || error === null || !("code" in error)) {
        return false;
    }
    return typeof error.code === "string" && ABSENT_FILE_ERROR_CODES.includes(error.code);
}

function ReadBatchEnvFile(): BatchEnvFile {
    try {
        const text = fs.readFileSync(BATCH_ENV_FILE, "utf8");
        return { text, values: parseEnv(text) };
    } catch (error) {
        if (IsAbsentFileError(error)) {
            return { text: "", values: {} };
        }
        throw error;
    }
}

// The lines python-dotenv reads as assigning name: optional leading whitespace and export, then
// the name, bare or in single quotes, then "=". The pattern is built from the fixed name only.
function AssignmentPattern(name: BatchVariable): RegExp {
    return new RegExp(
        String.raw`^[^\S\r\n]*(?:export[^\S\r\n]+)?(?:${name}|'${name}')[^\S\r\n]*=(.*)$`,
        "gm",
    );
}

// The text after "=" on the last line of backend/.env that assigns name, trimmed of spaces and
// tabs, or undefined when no line does. Both dotenv parsers keep the last assignment.
function LastAssignedText(text: string, name: BatchVariable): string | undefined {
    const assignments = [...text.matchAll(AssignmentPattern(name))];
    return assignments.at(-1)?.[1]?.replace(EDGE_BLANKS_PATTERN, "");
}

// pydantic-settings skips an empty value (env_ignore_empty), so the next source supplies it.
function IsNonEmpty(value: string | undefined): value is string {
    return value !== undefined && value !== "";
}

// A file value is plain when its last assignment is unquoted and parseEnv returns exactly its
// raw text. It then holds no "#" either, and python-dotenv reads the same text; the value
// checks refuse any edge whitespace other than spaces and tabs, which only python-dotenv trims.
function IsPlainAssignment(envFile: BatchEnvFile, name: BatchVariable): boolean {
    const assignedText = LastAssignedText(envFile.text, name);
    if (assignedText !== undefined && QUOTE_START_PATTERN.test(assignedText)) {
        return false;
    }
    return assignedText === envFile.values[name];
}

function ResolveBatchSetting(name: BatchVariable, envFile: BatchEnvFile): BatchSetting {
    const processValue = process.env[name];
    if (IsNonEmpty(processValue)) {
        return { name, value: processValue, source: PROCESS_ENV_SOURCE, isPlainLine: true };
    }
    const fileValue = envFile.values[name];
    const isPlainLine = IsPlainAssignment(envFile, name);
    if (IsNonEmpty(fileValue) || !isPlainLine) {
        return { name, value: fileValue, source: BATCH_ENV_FILE_SOURCE, isPlainLine };
    }
    return { name, value: BATCH_DEFAULTS[name], source: undefined, isPlainLine };
}

function DescribeAppEnvProblem(value: string): string | undefined {
    if (SEEDABLE_APP_ENVS.includes(value)) {
        return undefined;
    }
    return "is not local or test";
}

// The query keys make_url keeps and psycopg2 hands to libpq. parse_qsl splits on "&", decodes
// "%" and "+", and drops a pair whose value is empty or missing. The leading "&" keeps a "?"
// that opens the query in the first key, as parse_qsl does, where URLSearchParams drops it.
function QueryKeys(query: string): string[] {
    const pairs = [...new URLSearchParams(`&${query}`)];
    return pairs.filter(([, value]) => value !== "").map(([key]) => key);
}

// Regular expressions decide without raising, and no message holds the URL, which holds the
// database password.
function DescribeDatabaseUrlProblem(value: string): string | undefined {
    if (!PRINTABLE_ASCII_PATTERN.test(value)) {
        return "must hold printable ASCII characters only, with no spaces";
    }
    const parts = SQLALCHEMY_URL_PATTERN.exec(value)?.groups;
    if (parts === undefined || (parts.port !== undefined && !PORT_PATTERN.test(parts.port))) {
        return "is not a valid URL";
    }
    if ((parts.ipv4host ?? parts.ipv6host) !== LOCAL_HOSTNAME) {
        return `must use host ${LOCAL_HOSTNAME}`;
    }
    const queryKeys = parts.query === undefined ? [] : QueryKeys(parts.query);
    if (queryKeys.some((key) => HOST_OVERRIDE_PARAMETERS.includes(key))) {
        return "must not set host, hostaddr or service in its query";
    }
    return undefined;
}

function DescribeBatchValueProblem(
    setting: BatchSetting,
    value: string,
    describeValueProblem: (value: string) => string | undefined,
): string | undefined {
    if (setting.source === BATCH_ENV_FILE_SOURCE && value.includes(INTERPOLATION_MARKER)) {
        return `contains ${INTERPOLATION_MARKER}, which the batch CLI expands from other variables`;
    }
    return describeValueProblem(value);
}

// The message names the variable, its source and the rule broken, never the value: the
// database URL holds a password.
function RefuseBatchSetting(setting: BatchSetting, problem: string): never {
    const sourcePhrase = setting.source === undefined ? "" : ` from ${setting.source}`;
    throw new Error(`${setting.name}${sourcePhrase} ${problem}: ${BATCH_TARGET_HINT}`);
}

function RequireLocalBatchSetting(
    setting: BatchSetting,
    describeValueProblem: (value: string) => string | undefined,
): string {
    if (!setting.isPlainLine) {
        RefuseBatchSetting(setting, PLAIN_LINE_PROBLEM);
    }
    if (setting.value === undefined) {
        RefuseBatchSetting(setting, UNSET_PROBLEM);
    }
    const problem = DescribeBatchValueProblem(setting, setting.value, describeValueProblem);
    if (problem !== undefined) {
        RefuseBatchSetting(setting, problem);
    }
    return setting.value;
}

function RequireNoLibpqAddressOverride(): void {
    const setNames = LIBPQ_ADDRESS_VARIABLES.filter((name) => IsNonEmpty(process.env[name]));
    if (setNames.length > 0) {
        throw new Error(
            `${setNames.join(" and ")} from the environment must be unset, or libpq ignores the` +
                ` host of SYNC_DATABASE_URL: ${BATCH_TARGET_HINT}`,
        );
    }
}

// The batch CLI refuses production only at its DUSRSECJ step, after the earlier loaders have
// replaced their tables. So before any command, the settings the CLI would read must name a
// local, non-production database. They are returned, checked, for the command to be given.
function RequireLocalBatchTarget(): BatchTarget {
    const envFile = ReadBatchEnvFile();
    const appEnv = RequireLocalBatchSetting(
        ResolveBatchSetting("APP_ENV", envFile),
        DescribeAppEnvProblem,
    );
    const syncDatabaseUrl = RequireLocalBatchSetting(
        ResolveBatchSetting("SYNC_DATABASE_URL", envFile),
        DescribeDatabaseUrlProblem,
    );
    RequireNoLibpqAddressOverride();
    return { appEnv, syncDatabaseUrl };
}

// BatchSettings takes a nonempty process value over backend/.env, so the command uses exactly
// the target checked here, whatever its own dotenv parser would read from the file.
function BatchCliEnvironment(): NodeJS.ProcessEnv {
    const target = RequireLocalBatchTarget();
    return {
        ...process.env,
        APP_ENV: target.appEnv,
        SYNC_DATABASE_URL: target.syncDatabaseUrl,
        CARDDEMO_OUTPUT_DIR: OutputDir(),
    };
}

// Destructive, as docs/README.md warns, to the database SYNC_DATABASE_URL names, which
// RunBatchCli confirms is on host 127.0.0.1 with APP_ENV local or test before it starts the CLI:
// seed-all abandons the open batch cycle and reloads every loader, users included. TRANFILE
// loads 0 rows, so transactions.transactions is left empty and the first online add of
// day-in-the-life.spec.ts receives TRAN-ID 0000000000000001.
function SeedSampleData(): void {
    const SEED_ARGS = ["seed-all"] as const;
    const run = RunBatchCli(SEED_ARGS);
    if (run.status !== EXIT_SUCCESS) {
        throw new Error(DescribeBatchRun(SEED_ARGS, run));
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
 * Validates the settings, the two origins and the batch CLI's database target, then waits for
 * the backend and seeds the sample data: no request is made and no command runs until every
 * check has passed. The returned function is Playwright's global teardown: once every test has
 * finished, it copies each failed test's screenshots to blitzy/screenshots and videos to
 * blitzy/screen_recordings.
 */
export default async function GlobalSetup(config: FullConfig): Promise<() => void> {
    ValidateEnvironment();
    ValidateLocalOrigins();
    RequireLocalBatchTarget();
    await WaitForBackendReady();
    SeedSampleData();
    return () => {
        PublishFailureEvidence(config);
    };
}
