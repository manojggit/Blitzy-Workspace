/** @jest-environment node */
// Tests: e2e/global-setup.ts, the Playwright global setup that runs carddemo-batch seed-all in
// place of the CLOSEFIL, IDCAMS loader and OPENFIL sequence. Before any readiness request or
// batch command, it refuses e2e origins that are not one scheme on host 127.0.0.1, and a batch
// target whose APP_ENV is not local or test or whose SYNC_DATABASE_URL, as SQLAlchemy parses
// it, does not connect to host 127.0.0.1. Both are read as the CLI reads them, from the
// environment or else from plain unquoted backend/.env lines, and any other backend/.env form
// is refused. No refusal repeats a database URL or its credentials. A local target is seeded
// exactly once, and every carddemo-batch command is given the checked APP_ENV and
// SYNC_DATABASE_URL.
import type * as ChildProcessModule from "node:child_process";
import { spawnSync, type SpawnSyncReturns } from "node:child_process";
import fs from "node:fs";
import type * as FsModule from "node:fs";
import path from "node:path";

import type { FullConfig } from "@playwright/test";

import GlobalSetup, { RunBatchCli } from "../../e2e/global-setup";

jest.mock("node:child_process", () => ({
    ...jest.requireActual<typeof ChildProcessModule>("node:child_process"),
    spawnSync: jest.fn(),
}));

// The Playwright runner bundle does not load inside Jest. Global setup takes only the expect
// of the specs' helpers from it at run time, and GlobalSetup never calls that.
jest.mock("@playwright/test", () => ({ expect: jest.fn() }));

// Only reads of backend/.env are stubbed, in beforeEach; every other path reaches the real file
// system.
jest.mock("node:fs", () => {
    const actualFs = jest.requireActual<typeof FsModule>("node:fs");
    return { ...actualFs, readFileSync: jest.fn(actualFs.readFileSync) };
});

type Settings = Readonly<Record<string, string | undefined>>;

/** One global-setup run: variable overrides and the backend/.env content (undefined: absent). */
interface SetupCase {
    label: string;
    settings: Settings;
    envFile: string | undefined;
}

/** A run global setup must refuse, with the parts its message must contain. */
interface RefusalCase extends SetupCase {
    expectedParts: readonly string[];
}

/** A run global setup must accept, with the target the seed-all command must be given. */
interface AcceptedCase extends SetupCase {
    expectedTarget: { APP_ENV: string; SYNC_DATABASE_URL: string };
}

const ACTUAL_FS = jest.requireActual<typeof FsModule>("node:fs");
const BACKEND_ENV_FILE = path.resolve(__dirname, "../../../backend/.env");
const MINIMAL_CONFIG = { projects: [] } as unknown as FullConfig;
const SEED_ARGS = ["seed-all"];
const SEED_SUCCESS: SpawnSyncReturns<string> = {
    pid: 0,
    output: [null, "", ""],
    stdout: "",
    stderr: "",
    status: 0,
    signal: null,
};

const LOCAL_PAGE_ORIGIN = "http://127.0.0.1:20331";
const LOCAL_API_ORIGIN = "http://127.0.0.1:20330";
const REMOTE_DATABASE_HOST = "db.example.com";
const REMOTE_HOST_ADDRESS = "198.51.100.7";
const REMOTE_SERVICE = "remote_service";
// Assembled at run time, so no credential-shaped literal sits in the source.
const DATABASE_USER = ["e2e", "owner"].join("_");
const DATABASE_PASSWORD = ["unit", "test", "only"].join("-");
const SIGN_ON_SECRET = ["sign", "on", "value"].join("-");
const LOCAL_DATABASE_URL = DatabaseUrl("127.0.0.1:5432");
const REMOTE_DATABASE_URL = DatabaseUrl(`${REMOTE_DATABASE_HOST}:5432`);
// A backend/.env reference to another variable, which python-dotenv would expand.
const INTERPOLATED_DATABASE_URL = LOCAL_DATABASE_URL.replace(
    DATABASE_PASSWORD,
    ["$", "{DB_PASSWORD}"].join(""),
);
// A WHATWG URL reads "#..." as a fragment on host 127.0.0.1. SQLAlchemy's make_url keeps "#" in
// the database name and reads the query after it, so the batch CLI connects to its host.
const FRAGMENT_QUERY_URL = [
    "postgresql+psycopg2://127.0.0.1/carddemo",
    `#?host=${REMOTE_HOST_ADDRESS}&dbname=carddemo`,
].join("");
// No refusal message may repeat a URL, a credential, a remote host or a refused APP_ENV value.
const VALUE_PARTS = [
    DATABASE_USER,
    DATABASE_PASSWORD,
    "postgresql",
    REMOTE_DATABASE_HOST,
    REMOTE_HOST_ADDRESS,
    REMOTE_SERVICE,
    "production",
    "staging",
];

// Every variable ValidateEnvironment requires, with synthetic sign-on values that match no user.
const E2E_SETTINGS: Settings = {
    CARDDEMO_E2E_BASE_URL: LOCAL_PAGE_ORIGIN,
    CARDDEMO_E2E_API_URL: LOCAL_API_ORIGIN,
    CARDDEMO_E2E_USER_ID: "E2EREG01",
    CARDDEMO_E2E_PASSWORD: SIGN_ON_SECRET,
    CARDDEMO_E2E_ADMIN_ID: "E2EADM01",
    CARDDEMO_E2E_ADMIN_PASSWORD: SIGN_ON_SECRET,
    CARDDEMO_BATCH_BIN: ".venv/bin/carddemo-batch",
    CARDDEMO_OUTPUT_DIR: "var/batch-output",
};
const LOCAL_TARGET: Settings = {
    APP_ENV: "local",
    SYNC_DATABASE_URL: LOCAL_DATABASE_URL,
    PGHOSTADDR: undefined,
    PGSERVICE: undefined,
};
const UNSET_TARGET: Settings = { APP_ENV: undefined, SYNC_DATABASE_URL: undefined };
const TOUCHED_VARIABLES = [...Object.keys(E2E_SETTINGS), ...Object.keys(LOCAL_TARGET)];

const HOST_REFUSAL = "must use host 127.0.0.1";
const SCHEME_REFUSAL = "must use the same scheme";
const ORIGIN_REFUSALS: RefusalCase[] = [
    {
        label: "a localhost pair",
        settings: {
            CARDDEMO_E2E_BASE_URL: "http://localhost:20331",
            CARDDEMO_E2E_API_URL: "http://localhost:20330",
        },
        envFile: "",
        expectedParts: ["CARDDEMO_E2E_BASE_URL", "CARDDEMO_E2E_API_URL", HOST_REFUSAL],
    },
    {
        label: "an identical non-loopback host",
        settings: {
            CARDDEMO_E2E_BASE_URL: "http://192.0.2.10:20331",
            CARDDEMO_E2E_API_URL: "http://192.0.2.10:20330",
        },
        envFile: "",
        expectedParts: ["CARDDEMO_E2E_BASE_URL", "CARDDEMO_E2E_API_URL", HOST_REFUSAL],
    },
    {
        label: "an IPv6 loopback pair",
        settings: {
            CARDDEMO_E2E_BASE_URL: "http://[::1]:20331",
            CARDDEMO_E2E_API_URL: "http://[::1]:20330",
        },
        envFile: "",
        expectedParts: ["CARDDEMO_E2E_BASE_URL", "CARDDEMO_E2E_API_URL", HOST_REFUSAL],
    },
    {
        label: "a 127.0.0.1 page with a localhost API",
        settings: { CARDDEMO_E2E_API_URL: "http://localhost:20330" },
        envFile: "",
        expectedParts: ["CARDDEMO_E2E_API_URL (host localhost)", HOST_REFUSAL],
    },
    {
        label: "an http page with an https API",
        settings: { CARDDEMO_E2E_API_URL: "https://127.0.0.1:20330" },
        envFile: "",
        expectedParts: ["CARDDEMO_E2E_BASE_URL", "CARDDEMO_E2E_API_URL", SCHEME_REFUSAL],
    },
];

const APP_ENV_REFUSAL = "APP_ENV from the environment is not local or test";
const QUERY_REFUSAL =
    "SYNC_DATABASE_URL from the environment must not set host, hostaddr or service in its query";
const PRINTABLE_ASCII_REFUSAL =
    "SYNC_DATABASE_URL from the environment must hold printable ASCII characters only";
const ENVIRONMENT_TARGET_REFUSALS: RefusalCase[] = [
    {
        label: "APP_ENV production",
        settings: { APP_ENV: "production" },
        envFile: "",
        expectedParts: [APP_ENV_REFUSAL],
    },
    {
        label: "an unknown APP_ENV",
        settings: { APP_ENV: "staging" },
        envFile: "",
        expectedParts: [APP_ENV_REFUSAL],
    },
    {
        label: "a remote database host",
        settings: { SYNC_DATABASE_URL: REMOTE_DATABASE_URL },
        envFile: "",
        expectedParts: ["SYNC_DATABASE_URL from the environment must use host 127.0.0.1"],
    },
    {
        label: "an unparsable database URL",
        settings: { SYNC_DATABASE_URL: DatabaseUrl("127.0.0.1:port") },
        envFile: "",
        expectedParts: ["SYNC_DATABASE_URL from the environment is not a valid URL"],
    },
    ...["host", "hostaddr", "service"].map((parameter) => ({
        label: `a ?${parameter}= override`,
        settings: { SYNC_DATABASE_URL: DatabaseUrl("127.0.0.1:5432", `?${parameter}=elsewhere`) },
        envFile: "",
        expectedParts: [QUERY_REFUSAL],
    })),
    {
        label: "a query after a '#', which WHATWG reads as a fragment",
        settings: { SYNC_DATABASE_URL: FRAGMENT_QUERY_URL },
        envFile: "",
        expectedParts: [QUERY_REFUSAL],
    },
    {
        label: "a percent-encoded host key",
        settings: { SYNC_DATABASE_URL: DatabaseUrl("127.0.0.1:5432", "?ho%73t=elsewhere") },
        envFile: "",
        expectedParts: [QUERY_REFUSAL],
    },
    {
        label: "a second '@', which leaves SQLAlchemy a host before 127.0.0.1",
        settings: { SYNC_DATABASE_URL: DatabaseUrl(`${REMOTE_DATABASE_HOST}@127.0.0.1:5432`) },
        envFile: "",
        expectedParts: ["SYNC_DATABASE_URL from the environment must use host 127.0.0.1"],
    },
    {
        label: "a space in the database URL",
        settings: { SYNC_DATABASE_URL: DatabaseUrl("127.0.0.1:5432", "?application_name=e2e run") },
        envFile: "",
        expectedParts: [PRINTABLE_ASCII_REFUSAL],
    },
    {
        label: "a non-ASCII database host",
        settings: { SYNC_DATABASE_URL: DatabaseUrl("127.0.0.\uFF11:5432") },
        envFile: "",
        expectedParts: [PRINTABLE_ASCII_REFUSAL],
    },
    {
        label: "PGHOSTADDR",
        settings: { PGHOSTADDR: REMOTE_HOST_ADDRESS },
        envFile: "",
        expectedParts: ["PGHOSTADDR from the environment must be unset"],
    },
    {
        label: "PGSERVICE",
        settings: { PGSERVICE: REMOTE_SERVICE },
        envFile: "",
        expectedParts: ["PGSERVICE from the environment must be unset"],
    },
];

const UNSET_DATABASE_URL_REFUSAL =
    "SYNC_DATABASE_URL is set in neither the environment nor backend/.env";
const ENV_FILE_TARGET_REFUSALS: RefusalCase[] = [
    {
        label: "APP_ENV production",
        settings: UNSET_TARGET,
        envFile: `APP_ENV=production\nSYNC_DATABASE_URL=${LOCAL_DATABASE_URL}\n`,
        expectedParts: ["APP_ENV from backend/.env is not local or test"],
    },
    {
        label: "APP_ENV production under empty process values",
        settings: { APP_ENV: "", SYNC_DATABASE_URL: "" },
        envFile: `APP_ENV=production\nSYNC_DATABASE_URL=${LOCAL_DATABASE_URL}\n`,
        expectedParts: ["APP_ENV from backend/.env is not local or test"],
    },
    {
        label: "a remote database host",
        settings: UNSET_TARGET,
        envFile: `APP_ENV=local\nSYNC_DATABASE_URL=${REMOTE_DATABASE_URL}\n`,
        expectedParts: ["SYNC_DATABASE_URL from backend/.env must use host 127.0.0.1"],
    },
    {
        label: "a database URL that references another variable",
        settings: UNSET_TARGET,
        envFile: `SYNC_DATABASE_URL=${INTERPOLATED_DATABASE_URL}\n`,
        expectedParts: ["SYNC_DATABASE_URL from backend/.env contains ${"],
    },
    {
        label: "no database URL",
        settings: UNSET_TARGET,
        envFile: "APP_ENV=local\n",
        expectedParts: [UNSET_DATABASE_URL_REFUSAL],
    },
    {
        label: "no file and no database URL",
        settings: UNSET_TARGET,
        envFile: undefined,
        expectedParts: [UNSET_DATABASE_URL_REFUSAL],
    },
];

// Lines python-dotenv, the batch CLI's parser, may read differently from Node's parseEnv.
const PLAIN_LINE_RULE =
    "must be a plain unquoted value with no comment, so global setup reads it as the batch" +
    " CLI does";
const PLAIN_APP_ENV_REFUSAL = `APP_ENV from backend/.env ${PLAIN_LINE_RULE}`;
const PLAIN_DATABASE_URL_REFUSAL = `SYNC_DATABASE_URL from backend/.env ${PLAIN_LINE_RULE}`;
const LOCAL_URL_LINE = `SYNC_DATABASE_URL=${LOCAL_DATABASE_URL}\n`;
const REMOTE_URL_LINE = `SYNC_DATABASE_URL=${REMOTE_DATABASE_URL}\n`;
const ENV_FILE_FORM_REFUSALS: RefusalCase[] = [
    {
        label: "a '#' that parseEnv cuts and python-dotenv keeps",
        envFile: `APP_ENV=local\nSYNC_DATABASE_URL=${FRAGMENT_QUERY_URL}\n`,
        expectedParts: [PLAIN_DATABASE_URL_REFUSAL],
    },
    {
        label: "an inline comment",
        envFile: `APP_ENV=local # note\n${LOCAL_URL_LINE}`,
        expectedParts: [PLAIN_APP_ENV_REFUSAL],
    },
    {
        label: "a double-quoted value",
        envFile: `SYNC_DATABASE_URL="${LOCAL_DATABASE_URL}"\n`,
        expectedParts: [PLAIN_DATABASE_URL_REFUSAL],
    },
    {
        label: "a single-quoted value",
        envFile: `APP_ENV='test'\n${LOCAL_URL_LINE}`,
        expectedParts: [PLAIN_APP_ENV_REFUSAL],
    },
    {
        label: "an unclosed quote",
        envFile: `APP_ENV="local\n${LOCAL_URL_LINE}`,
        expectedParts: [PLAIN_APP_ENV_REFUSAL],
    },
    {
        label: "a backtick value that hides a later assignment from parseEnv only",
        envFile: `${LOCAL_URL_LINE}NOTE=\`\n${REMOTE_URL_LINE}\`\n`,
        expectedParts: [PLAIN_DATABASE_URL_REFUSAL],
    },
    {
        label: "a multi-line double-quoted value that shadows an assignment",
        envFile: `${LOCAL_URL_LINE}NOTE="\n${REMOTE_URL_LINE}"\n`,
        expectedParts: [PLAIN_DATABASE_URL_REFUSAL],
    },
    {
        label: "a multi-line double-quoted value that hides the only assignment",
        envFile: `NOTE="\n${LOCAL_URL_LINE}"\n`,
        expectedParts: [PLAIN_DATABASE_URL_REFUSAL],
    },
    {
        label: "a single-quoted name, which only python-dotenv reads",
        envFile: `${LOCAL_URL_LINE}'SYNC_DATABASE_URL'=${REMOTE_DATABASE_URL}\n`,
        expectedParts: [PLAIN_DATABASE_URL_REFUSAL],
    },
    {
        label: "export and a tab, which only python-dotenv reads",
        envFile: `${LOCAL_URL_LINE}export\t${REMOTE_URL_LINE}`,
        expectedParts: [PLAIN_DATABASE_URL_REFUSAL],
    },
    {
        label: "a no-break space before the name, which only python-dotenv skips",
        envFile: `${LOCAL_URL_LINE}\u00A0${REMOTE_URL_LINE}`,
        expectedParts: [PLAIN_DATABASE_URL_REFUSAL],
    },
].map((refusal) => ({ ...refusal, settings: UNSET_TARGET }));

const ACCEPTED_ORIGIN_PAIRS = [
    { pageOrigin: LOCAL_PAGE_ORIGIN, apiOrigin: LOCAL_API_ORIGIN },
    { pageOrigin: "https://127.0.0.1:20331", apiOrigin: "https://127.0.0.1:20330" },
];
const ACCEPTED_APP_ENVS = ["local", "test", undefined];
const ACCEPTED_CASES: AcceptedCase[] = [
    ...ACCEPTED_ORIGIN_PAIRS.flatMap(({ pageOrigin, apiOrigin }) =>
        ACCEPTED_APP_ENVS.map((appEnv) => ({
            label: `${pageOrigin} with ${apiOrigin}, APP_ENV ${appEnv ?? "unset"}`,
            settings: {
                CARDDEMO_E2E_BASE_URL: pageOrigin,
                CARDDEMO_E2E_API_URL: apiOrigin,
                APP_ENV: appEnv,
            },
            envFile: "",
            expectedTarget: { APP_ENV: appEnv ?? "local", SYNC_DATABASE_URL: LOCAL_DATABASE_URL },
        })),
    ),
    {
        label: "a local target read from backend/.env",
        settings: { APP_ENV: "", SYNC_DATABASE_URL: undefined },
        envFile: `APP_ENV=test\n${LOCAL_URL_LINE}`,
        expectedTarget: { APP_ENV: "test", SYNC_DATABASE_URL: LOCAL_DATABASE_URL },
    },
    {
        label: "an APP_ENV set nowhere and a database URL read from backend/.env",
        settings: UNSET_TARGET,
        envFile: LOCAL_URL_LINE,
        expectedTarget: { APP_ENV: "local", SYNC_DATABASE_URL: LOCAL_DATABASE_URL },
    },
    {
        label: "plain backend/.env lines with export, blanks, comments and test URLs",
        settings: UNSET_TARGET,
        envFile: [
            `# ${REMOTE_URL_LINE}`,
            "export APP_ENV = test\n",
            `SYNC_DATABASE_URL=\t${LOCAL_DATABASE_URL} \n`,
            `TEST_${REMOTE_URL_LINE}`,
        ].join(""),
        expectedTarget: { APP_ENV: "test", SYNC_DATABASE_URL: LOCAL_DATABASE_URL },
    },
    {
        label: "process values over a production, remote backend/.env",
        settings: { APP_ENV: "test" },
        envFile: `APP_ENV=production\n${REMOTE_URL_LINE}`,
        expectedTarget: { APP_ENV: "test", SYNC_DATABASE_URL: LOCAL_DATABASE_URL },
    },
];

// RunBatchCli, which the specs also call, checks the target itself before any command.
const RUN_ALL_ARGS = ["run-all", "--start-at", "POSTTRAN"];
const BATCH_CLI_REFUSALS: RefusalCase[] = [
    {
        label: "a query after a '#' in the environment",
        settings: { SYNC_DATABASE_URL: FRAGMENT_QUERY_URL },
        envFile: "",
        expectedParts: [QUERY_REFUSAL],
    },
    {
        label: "a '#' in backend/.env",
        settings: UNSET_TARGET,
        envFile: `SYNC_DATABASE_URL=${FRAGMENT_QUERY_URL}\n`,
        expectedParts: [PLAIN_DATABASE_URL_REFUSAL],
    },
];

const spawnSyncMock = jest.mocked(spawnSync);
const readFileSyncMock = jest.mocked(fs.readFileSync);

let savedSettings: Settings = {};
let envFileContent: string | undefined;
let fetchMock: jest.SpiedFunction<typeof fetch>;

function DatabaseUrl(hostAndPort: string, query = ""): string {
    const userInfo = [DATABASE_USER, DATABASE_PASSWORD].join(":");
    return `postgresql+psycopg2://${userInfo}@${hostAndPort}/carddemo_e2e${query}`;
}

// backend/.env holds envFileContent, or is absent while that is undefined.
function StubReadFileSync(
    file: FsModule.PathOrFileDescriptor,
    options?: Parameters<typeof FsModule.readFileSync>[1],
): ReturnType<typeof FsModule.readFileSync> {
    if (file !== BACKEND_ENV_FILE) {
        return ACTUAL_FS.readFileSync(file, options);
    }
    if (envFileContent === undefined) {
        throw Object.assign(new Error("backend/.env is absent"), { code: "ENOENT" });
    }
    return envFileContent;
}

function RespondReady(): Promise<Response> {
    return Promise.resolve(new Response(JSON.stringify({ status: "ready" }), { status: 200 }));
}

function SetVariable(name: string, value: string | undefined): void {
    if (value === undefined) {
        delete process.env[name];
    } else {
        process.env[name] = value;
    }
}

function ApplySettings(settings: Settings): void {
    for (const [name, value] of Object.entries(settings)) {
        SetVariable(name, value);
    }
}

function ApplyCase({ settings, envFile }: SetupCase): void {
    ApplySettings({ ...E2E_SETTINGS, ...LOCAL_TARGET, ...settings });
    envFileContent = envFile;
}

async function ExpectRefusal(refusal: RefusalCase): Promise<void> {
    ApplyCase(refusal);
    const setup = GlobalSetup(MINIMAL_CONFIG);
    for (const part of refusal.expectedParts) {
        await expect(setup).rejects.toThrow(part);
    }
    for (const part of VALUE_PARTS) {
        await expect(setup).rejects.not.toThrow(part);
    }
    expect(fetchMock).not.toHaveBeenCalled();
    expect(spawnSyncMock).not.toHaveBeenCalled();
}

// The command is given the checked values, which BatchSettings takes over backend/.env.
async function ExpectSeededOnce(accepted: AcceptedCase): Promise<void> {
    ApplyCase(accepted);
    await expect(GlobalSetup(MINIMAL_CONFIG)).resolves.toEqual(expect.any(Function));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(spawnSyncMock).toHaveBeenCalledTimes(1);
    expect(spawnSyncMock.mock.calls[0]?.[1]).toEqual(SEED_ARGS);
    expect(spawnSyncMock.mock.calls[0]?.[2]?.env).toMatchObject(accepted.expectedTarget);
}

function ExpectBatchCliRefusal(refusal: RefusalCase): void {
    ApplyCase(refusal);
    for (const part of refusal.expectedParts) {
        expect(() => RunBatchCli(RUN_ALL_ARGS)).toThrow(part);
    }
    for (const part of VALUE_PARTS) {
        expect(() => RunBatchCli(RUN_ALL_ARGS)).not.toThrow(part);
    }
    expect(spawnSyncMock).not.toHaveBeenCalled();
}

beforeEach(() => {
    savedSettings = Object.fromEntries(TOUCHED_VARIABLES.map((name) => [name, process.env[name]]));
    readFileSyncMock.mockImplementation(StubReadFileSync);
    spawnSyncMock.mockReturnValue(SEED_SUCCESS);
    fetchMock = jest.spyOn(globalThis, "fetch").mockImplementation(RespondReady);
});

afterEach(() => {
    ApplySettings(savedSettings);
    fetchMock.mockRestore();
});

it.each(ORIGIN_REFUSALS)("refuses $label before any request or command", ExpectRefusal);

it.each(ENVIRONMENT_TARGET_REFUSALS)(
    "refuses $label in the environment before any request or command",
    ExpectRefusal,
);

it.each(ENV_FILE_TARGET_REFUSALS)(
    "refuses $label in backend/.env before any request or command",
    ExpectRefusal,
);

it.each(ENV_FILE_FORM_REFUSALS)(
    "refuses $label in backend/.env before any request or command",
    ExpectRefusal,
);

it.each(ACCEPTED_CASES)("seeds once, with the checked target, for $label", ExpectSeededOnce);

it.each(BATCH_CLI_REFUSALS)(
    "refuses $label in RunBatchCli without starting the command",
    ExpectBatchCliRefusal,
);
