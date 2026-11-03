// Tests: lib/apiClient.ts (replaces the COCOM01Y COMMAREA hand-off, the COSGN00C XCTL to the
// user's own menu, the CSMSG01Y/CSMSG02Y message areas and the BMS ERRMSG line contract).
import axios, {
    AxiosError,
    AxiosHeaders,
    CanceledError,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";

import {
    ABEND_MESSAGE,
    ADMIN_HOME_ROUTE,
    API_CLIENT,
    API_V1_PATH,
    API_V1_URL,
    ClearHomeRoute,
    DEFAULT_HOME_ROUTE,
    EncodePathKey,
    GetHomeRoute,
    HandleResponse,
    HandleResponseError,
    HOME_ROUTE_ALLOWANCE,
    HOME_ROUTE_KEY,
    IsCancelledRequest,
    LegacyErrorBody,
    MenuTreeHomeRoute,
    PENDING_MESSAGE_KEY,
    PENDING_MESSAGE_ROUTE_KEY,
    REQUEST_TIMEOUT_MILLISECONDS,
    SeverityForOutcome,
    SetHomeRoute,
    SIGN_ON_ROUTE,
    StorePendingMessage,
    TakePendingMessage,
    ToAllowedRoute,
    ToScreenFeedback,
    UpdateScreenState,
    type LegacyErrorResponse,
    type MessageSeverity,
    type RouteAllowance,
} from "@/lib/apiClient";

// jsdom implements no navigation: every window.location.assign is reported through
// console.error, because window.location cannot be mocked under jsdom 26.
const NAVIGATION_NOT_IMPLEMENTED = "Not implemented: navigation";
const CURRENT_USER_URL = API_V1_URL + "/auth/me";
const BACKEND_ORIGIN = "http://127.0.0.1:8000";
const ACCOUNT_URL = "/accounts/00000000001";
const ADMIN_USERS_URL = "/admin/users";
const CHANGES_FAILED_INFO = "Changes unsuccessful. Please try again";
// COMEN01C keeps the trailing space of its literal (cbl/COMEN01C.cbl line 140).
const ADMIN_ONLY_MESSAGE = "No access - Admin Only option... ";
// The agreed bound on every call, pinned here rather than read back from the client. It must stay
// a finite whole number of milliseconds longer than one backend statement (the backend's default
// DB_COMMAND_TIMEOUT_SECONDS is 30), because axios reads 0 as no bound at all.
const AGREED_REQUEST_TIMEOUT_MILLISECONDS = 60000;
const BACKEND_STATEMENT_TIMEOUT_MILLISECONDS = 30000;
const EXPECTED_LOOKUP_OPTIONS = {
    withCredentials: true,
    timeout: AGREED_REQUEST_TIMEOUT_MILLISECONDS,
};

function BuildRequestConfig(url: string, method = "get"): InternalAxiosRequestConfig {
    return { url, method, headers: new AxiosHeaders() };
}

function BuildAxiosResponse(
    status: number,
    data: unknown,
    config: InternalAxiosRequestConfig,
): AxiosResponse {
    return { status, data, statusText: "", headers: {}, config };
}

function BuildAxiosError(status: number, body: unknown, url: string): AxiosError {
    const config = BuildRequestConfig(url);
    const response = BuildAxiosResponse(status, body, config);
    return new AxiosError(
        "Request failed",
        AxiosError.ERR_BAD_RESPONSE,
        config,
        undefined,
        response,
    );
}

function BuildNetworkError(url: string): AxiosError {
    return new AxiosError("Network Error", AxiosError.ERR_NETWORK, BuildRequestConfig(url));
}

// What axios raises for a call that outlives its timeout: the call's own config and no response,
// with ECONNABORTED, or ETIMEDOUT under transitional.clarifyTimeoutError.
function BuildTimeoutError(code: string, config = BuildRequestConfig(ACCOUNT_URL)): AxiosError {
    const message = `timeout of ${AGREED_REQUEST_TIMEOUT_MILLISECONDS}ms exceeded`;
    return new AxiosError(message, code, config);
}

function BuildLegacyBody(
    message: string,
    cursorField: string | null,
    infoMessage = "",
): LegacyErrorResponse {
    const fieldErrors = cursorField === null ? [] : [cursorField];
    return {
        message,
        info_message: infoMessage,
        field_errors: fieldErrors,
        cursor_field: cursorField,
    };
}

const ADMIN_ONLY_BODY = BuildLegacyBody(ADMIN_ONLY_MESSAGE, null);
// An admin calling a regular endpoint is refused with an empty message line.
const EMPTY_REFUSAL_BODY = BuildLegacyBody("", null);
const ABEND_FEEDBACK = {
    message: ABEND_MESSAGE,
    info_message: "",
    field_errors: [],
    cursor_field: "",
};
const LOGIN_RESPONSE = BuildAxiosResponse(200, {}, BuildRequestConfig("/auth/login", "post"));

// The sign-on page shows its own 401 literals (Wrong Password. Try again ...), so login, logout
// and the sign-on page itself are never redirected.
const UNAUTHORIZED_CASES: readonly { url: string; currentPath: string; navigates: boolean }[] = [
    { url: ACCOUNT_URL, currentPath: "/", navigates: true },
    { url: "/auth/me", currentPath: DEFAULT_HOME_ROUTE, navigates: true },
    { url: "/auth/login", currentPath: SIGN_ON_ROUTE, navigates: false },
    { url: API_V1_URL + "/auth/login", currentPath: DEFAULT_HOME_ROUTE, navigates: false },
    { url: "/auth/logout", currentPath: DEFAULT_HOME_ROUTE, navigates: false },
    { url: "/cards", currentPath: SIGN_ON_ROUTE, navigates: false },
];

const LEGACY_ERROR_CASES: readonly { status: number; body: LegacyErrorResponse }[] = [
    { status: 400, body: BuildLegacyBody("Account Status must be Y or N.", "active_status") },
    { status: 404, body: BuildLegacyBody("Account ID NOT found...", "acct_id") },
    { status: 409, body: BuildLegacyBody("Record changed by some one else. Please review", null) },
    {
        status: 423,
        body: BuildLegacyBody(
            "Could not lock account record for update",
            null,
            CHANGES_FAILED_INFO,
        ),
    },
    { status: 429, body: BuildLegacyBody("Too many failed attempts. Try again later ...", null) },
    { status: 503, body: BuildLegacyBody("Unable to lookup Account...", "acct_id") },
];

const ABEND_CASES: readonly { label: string; error: AxiosError }[] = [
    {
        label: "500 with a legacy body",
        error: BuildAxiosError(500, BuildLegacyBody("Update of record failed", null), ACCOUNT_URL),
    },
    { label: "502 proxy page", error: BuildAxiosError(502, "<html>Bad Gateway</html>", "/cards") },
    { label: "network failure", error: BuildNetworkError("/billpay") },
    { label: "timeout", error: BuildTimeoutError(AxiosError.ECONNABORTED) },
];

const VALIDATION_DETAIL = [
    { loc: ["body", "desc"], msg: "Value error, code page 037", type: "value_error" },
];

// FastAPI's 422 body (common.LegacyValidationErrorResponse): the detail list beside a blank
// message, with the case's field_errors and cursor_field keys as the server sent them.
function BuildRejection(feedbackKeys: Record<string, unknown>, url: string): AxiosError {
    const body = { detail: VALIDATION_DETAIL, message: "", info_message: "", ...feedbackKeys };
    return BuildAxiosError(422, body, url);
}

// A 422 structural rejection has a blank message, so it shows the abend text, yet every input its
// field_errors names is marked and the cursor goes to its cursor_field. A key that is absent or
// of another shape names no input.
const STRUCTURAL_REJECTION_CASES: readonly {
    label: string;
    error: AxiosError;
    fieldErrors: string[];
    cursorField: string | null;
}[] = [
    {
        label: "422 naming two CAUP inputs",
        error: BuildRejection(
            { field_errors: ["addr_line_1", "last_name"], cursor_field: "addr_line_1" },
            ACCOUNT_URL,
        ),
        fieldErrors: ["addr_line_1", "last_name"],
        cursorField: "addr_line_1",
    },
    {
        label: "422 naming the CR00 confirm query field",
        error: BuildRejection(
            { field_errors: ["confirm"], cursor_field: "confirm" },
            "/reports/transactions",
        ),
        fieldErrors: ["confirm"],
        cursorField: "confirm",
    },
    {
        label: "422 with a null cursor",
        error: BuildRejection({ field_errors: ["desc"], cursor_field: null }, "/transactions"),
        fieldErrors: ["desc"],
        cursorField: null,
    },
    {
        label: "422 for malformed JSON, which names no field",
        error: BuildRejection({ field_errors: [], cursor_field: "" }, "/billpay"),
        fieldErrors: [],
        cursorField: "",
    },
    {
        label: "422 without the legacy keys",
        error: BuildAxiosError(422, { detail: VALIDATION_DETAIL }, "/transactions"),
        fieldErrors: [],
        cursorField: "",
    },
    {
        label: "422 whose keys have another shape",
        error: BuildRejection({ field_errors: "desc", cursor_field: 7 }, "/transactions"),
        fieldErrors: [],
        cursorField: "",
    },
    {
        label: "422 whose field list holds a non-name",
        error: BuildRejection({ field_errors: ["desc", 3], cursor_field: "desc" }, "/transactions"),
        fieldErrors: [],
        cursorField: "desc",
    },
];

// The client's default and the exported constant, each held to the pinned bound and to the
// backend's statement bound.
const CONFIGURED_TIMEOUT_CASES: readonly { source: string; timeout: number | undefined }[] = [
    { source: "API_CLIENT.defaults.timeout", timeout: API_CLIENT.defaults.timeout },
    { source: "REQUEST_TIMEOUT_MILLISECONDS", timeout: REQUEST_TIMEOUT_MILLISECONDS },
];

// A timed-out read and a timed-out write, under both codes axios gives a timeout. A write's outcome
// is then unknown, since the server may still commit it, so neither is ever sent again.
const TIMEOUT_CASES: readonly { method: string; code: string }[] = [
    { method: "get", code: AxiosError.ECONNABORTED },
    { method: "get", code: AxiosError.ETIMEDOUT },
    { method: "put", code: AxiosError.ECONNABORTED },
    { method: "put", code: AxiosError.ETIMEDOUT },
];

// Only a call abandoned through its signal is a cancellation; every other failure keeps its
// legacy feedback or the abend text.
const CANCELLATION_CASES: readonly { label: string; error: unknown; isCancelled: boolean }[] = [
    { label: "axios CanceledError", error: new CanceledError(), isCancelled: true },
    { label: "500 response", error: BuildAxiosError(500, {}, ACCOUNT_URL), isCancelled: false },
    { label: "network failure", error: BuildNetworkError("/cards"), isCancelled: false },
    {
        label: "timeout",
        error: new AxiosError("timeout exceeded", AxiosError.ECONNABORTED),
        isCancelled: false,
    },
    { label: "plain Error", error: new Error("canceled"), isCancelled: false },
    { label: "non-error value", error: "canceled", isCancelled: false },
    { label: "undefined", error: undefined, isCancelled: false },
];

// A read aborted before it is sent, or while its answer is in flight, whose answer is discarded.
const ABORT_TIMING_CASES: readonly { timing: string; abortsInFlight: boolean }[] = [
    { timing: "before it is sent", abortsInFlight: false },
    { timing: "while its answer is in flight", abortsInFlight: true },
];

const SEVERITY_CASES: readonly {
    isError: boolean;
    isCompletedWrite: boolean;
    severity: MessageSeverity;
}[] = [
    { isError: true, isCompletedWrite: false, severity: "error" },
    { isError: true, isCompletedWrite: true, severity: "error" },
    { isError: false, isCompletedWrite: true, severity: "success" },
    { isError: false, isCompletedWrite: false, severity: "info" },
];

const LOGIN_RESPONSE_CASES: readonly {
    method: string;
    url: string;
    status: number;
    clears: boolean;
}[] = [
    { method: "post", url: "/auth/login", status: 200, clears: true },
    { method: "POST", url: API_V1_URL + "/auth/login", status: 204, clears: true },
    { method: "get", url: "/auth/me", status: 200, clears: false },
    { method: "post", url: "/auth/logout", status: 200, clears: false },
];

// The /auth/me next_route decides the target, whatever this tab recorded: a sign-on in another
// tab replaces the shared session cookie but not this tab's record. A refusal on the target itself
// does not reload, so a stale record is shown unused when a refusal on it still navigates and one
// on the fresh route does not.
const LOOKUP_CASES: readonly {
    recordedRoute: string | null;
    nextRoute: unknown;
    currentPath: string;
    navigates: boolean;
    storedRoute: string | null;
}[] = [
    {
        recordedRoute: null,
        nextRoute: ADMIN_HOME_ROUTE,
        currentPath: "/cards",
        navigates: true,
        storedRoute: ADMIN_HOME_ROUTE,
    },
    {
        recordedRoute: null,
        nextRoute: ADMIN_HOME_ROUTE,
        currentPath: ADMIN_HOME_ROUTE,
        navigates: false,
        storedRoute: ADMIN_HOME_ROUTE,
    },
    {
        recordedRoute: null,
        nextRoute: DEFAULT_HOME_ROUTE,
        currentPath: ADMIN_USERS_URL,
        navigates: true,
        storedRoute: DEFAULT_HOME_ROUTE,
    },
    {
        recordedRoute: null,
        nextRoute: ADMIN_USERS_URL,
        currentPath: DEFAULT_HOME_ROUTE,
        navigates: false,
        storedRoute: null,
    },
    {
        recordedRoute: null,
        nextRoute: undefined,
        currentPath: "/cards",
        navigates: true,
        storedRoute: null,
    },
    {
        recordedRoute: DEFAULT_HOME_ROUTE,
        nextRoute: ADMIN_HOME_ROUTE,
        currentPath: DEFAULT_HOME_ROUTE,
        navigates: true,
        storedRoute: ADMIN_HOME_ROUTE,
    },
    {
        recordedRoute: DEFAULT_HOME_ROUTE,
        nextRoute: ADMIN_HOME_ROUTE,
        currentPath: ADMIN_HOME_ROUTE,
        navigates: false,
        storedRoute: ADMIN_HOME_ROUTE,
    },
    {
        recordedRoute: ADMIN_HOME_ROUTE,
        nextRoute: DEFAULT_HOME_ROUTE,
        currentPath: ADMIN_HOME_ROUTE,
        navigates: true,
        storedRoute: DEFAULT_HOME_ROUTE,
    },
    {
        recordedRoute: ADMIN_HOME_ROUTE,
        nextRoute: DEFAULT_HOME_ROUTE,
        currentPath: DEFAULT_HOME_ROUTE,
        navigates: false,
        storedRoute: DEFAULT_HOME_ROUTE,
    },
];

// A failed lookup falls back to the route this tab recorded, or to the main menu without one. A
// refusal on the fallback route itself does not reload, which shows the route chosen.
const LOOKUP_FAILURE_CASES: readonly {
    failure: string;
    error: AxiosError;
    recordedRoute: string | null;
    currentPath: string;
    navigates: boolean;
}[] = [
    {
        failure: "network failure",
        error: BuildNetworkError(CURRENT_USER_URL),
        recordedRoute: null,
        currentPath: "/cards",
        navigates: true,
    },
    {
        failure: "network failure",
        error: BuildNetworkError(CURRENT_USER_URL),
        recordedRoute: null,
        currentPath: DEFAULT_HOME_ROUTE,
        navigates: false,
    },
    {
        failure: "timeout",
        error: BuildTimeoutError(AxiosError.ECONNABORTED, BuildRequestConfig(CURRENT_USER_URL)),
        recordedRoute: null,
        currentPath: "/cards",
        navigates: true,
    },
    {
        failure: "timeout",
        error: BuildTimeoutError(AxiosError.ETIMEDOUT, BuildRequestConfig(CURRENT_USER_URL)),
        recordedRoute: null,
        currentPath: DEFAULT_HOME_ROUTE,
        navigates: false,
    },
    {
        failure: "network failure",
        error: BuildNetworkError(CURRENT_USER_URL),
        recordedRoute: ADMIN_HOME_ROUTE,
        currentPath: DEFAULT_HOME_ROUTE,
        navigates: true,
    },
    {
        failure: "network failure",
        error: BuildNetworkError(CURRENT_USER_URL),
        recordedRoute: ADMIN_HOME_ROUTE,
        currentPath: ADMIN_HOME_ROUTE,
        navigates: false,
    },
    {
        failure: "a 503",
        error: BuildAxiosError(503, BuildLegacyBody("", null), CURRENT_USER_URL),
        recordedRoute: ADMIN_HOME_ROUTE,
        currentPath: ADMIN_HOME_ROUTE,
        navigates: false,
    },
];

const SESSION_END_CASES = [
    { ending: "sign-off", EndSession: (): void => ClearHomeRoute() },
    {
        ending: "a new login",
        EndSession: (): void => {
            HandleResponse(LOGIN_RESPONSE);
        },
    },
];

const HOME_ROUTE_CASES: readonly string[] = [DEFAULT_HOME_ROUTE, ADMIN_HOME_ROUTE];
const NON_HOME_ROUTES: readonly string[] = [
    "/admin/users",
    "/ADMIN",
    " /menu",
    "https://x.test/admin",
];
const STORAGE_REFUSAL_NAMES: readonly string[] = ["SecurityError", "QuotaExceededError"];
const ORIGIN_CASES: readonly string[] = [
    BACKEND_ORIGIN,
    BACKEND_ORIGIN + "/",
    BACKEND_ORIGIN + "//",
];
// A raw screen key as one path segment: "/" and "%" percent-encoded, and only a blank, "." or ".."
// key given the one trailing space the server's space padding ignores.
const PATH_KEY_CASES: readonly { key: string; segment: string }[] = [
    { key: "", segment: "%20" },
    { key: ".", segment: ".%20" },
    { key: "..", segment: "..%20" },
    { key: "A/B", segment: "A%2FB" },
    { key: "/", segment: "%2F" },
    { key: "%", segment: "%25" },
    { key: "00000000001", segment: "00000000001" },
    { key: "A.B", segment: "A.B" },
    { key: "A B  C", segment: "A%20B%20%20C" },
];
const TRANSACTIONS_URL = API_V1_PATH + "/transactions";
// What the WHATWG URL parser makes of a bare dot segment, which is why such a key is padded.
const DOT_SEGMENT_CASES: readonly { segment: string; pathname: string }[] = [
    { segment: ".", pathname: TRANSACTIONS_URL + "/" },
    { segment: "..", pathname: API_V1_PATH + "/" },
    { segment: "%2E%2E", pathname: API_V1_PATH + "/" },
];

let consoleErrorSpy: jest.SpyInstance;

function NavigationWasAttempted(spy: jest.SpyInstance): boolean {
    return spy.mock.calls.some((call: unknown[]) => {
        const firstArgument = call[0];
        const text = firstArgument instanceof Error ? firstArgument.message : String(firstArgument);
        return text.includes(NAVIGATION_NOT_IMPLEMENTED);
    });
}

async function ExpectRejectedUnchanged(error: AxiosError): Promise<void> {
    await expect(HandleResponseError(error)).rejects.toBe(error);
}

// Storage is read directly, because a message kept for another page is withheld from this one.
function ExpectNoRefusalEffects(): void {
    expect(NavigationWasAttempted(consoleErrorSpy)).toBe(false);
    expect(window.sessionStorage.getItem(PENDING_MESSAGE_KEY)).toBeNull();
}

function BuildLookupAnswer(nextRoute: unknown): AxiosResponse {
    return BuildAxiosResponse(200, { next_route: nextRoute }, BuildRequestConfig(CURRENT_USER_URL));
}

function MockHomeRouteLookup(nextRoute: unknown): jest.SpyInstance {
    return jest.spyOn(axios, "get").mockResolvedValue(BuildLookupAnswer(nextRoute));
}

// The home route this tab recorded before a refusal; null records none.
function RecordHomeRoute(route: string | null): void {
    if (route !== null) {
        SetHomeRoute(route);
    }
}

// The /auth/me answer arrives only when the returned function is called.
function DeferHomeRouteLookup(): (nextRoute: unknown) => void {
    let resolveLookup: (answer: AxiosResponse) => void = () => undefined;
    const pendingLookup = new Promise<AxiosResponse>((resolve) => {
        resolveLookup = resolve;
    });
    jest.spyOn(axios, "get").mockReturnValue(pendingLookup);
    return (nextRoute) => resolveLookup(BuildLookupAnswer(nextRoute));
}

function RefuseSessionStorage(error: Error): void {
    const refuse = (): never => {
        throw error;
    };
    jest.spyOn(Storage.prototype, "getItem").mockImplementation(refuse);
    jest.spyOn(Storage.prototype, "setItem").mockImplementation(refuse);
    jest.spyOn(Storage.prototype, "removeItem").mockImplementation(refuse);
}

function RestoreApiOrigin(previousOrigin: string | undefined): void {
    if (previousOrigin === undefined) {
        delete process.env.NEXT_PUBLIC_API_URL;
    } else {
        process.env.NEXT_PUBLIC_API_URL = previousOrigin;
    }
}

// The origin is read once, when the module loads, so each origin gets a fresh module registry.
async function LoadClientRoots(origin: string): Promise<(string | undefined)[]> {
    const previousOrigin = process.env.NEXT_PUBLIC_API_URL;
    process.env.NEXT_PUBLIC_API_URL = origin;
    const roots: (string | undefined)[] = [];
    try {
        await jest.isolateModulesAsync(async () => {
            const reloadedClient = await import("@/lib/apiClient");
            roots.push(reloadedClient.API_V1_URL, reloadedClient.API_CLIENT.defaults.baseURL);
        });
    } finally {
        RestoreApiOrigin(previousOrigin);
    }
    return roots;
}

beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    ClearHomeRoute();
    window.history.pushState({}, "", "/");
    consoleErrorSpy = jest.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
    jest.restoreAllMocks();
});

it("pins the API root, the ABEND-ROUTINE text and the sign-on and menu routes", () => {
    expect(API_V1_PATH).toBe("/api/v1");
    expect(ABEND_MESSAGE).toBe("UNEXPECTED ABEND OCCURRED.");
    expect(SIGN_ON_ROUTE).toBe("/signon");
    expect(DEFAULT_HOME_ROUTE).toBe("/menu");
    expect(ADMIN_HOME_ROUTE).toBe("/admin");
});

it("sends the session cookie on every call under /api/v1 (COMMAREA identity)", () => {
    expect(String(API_CLIENT.defaults.baseURL).endsWith(API_V1_PATH)).toBe(true);
    expect(API_CLIENT.defaults.baseURL).toBe(API_V1_URL);
    expect(API_CLIENT.defaults.withCredentials).toBe(true);
});

it.each(CONFIGURED_TIMEOUT_CASES)(
    "$source is the agreed 60 s bound, a whole number longer than one backend statement",
    ({ timeout }) => {
        expect(timeout).toBe(AGREED_REQUEST_TIMEOUT_MILLISECONDS);
        expect(Number.isSafeInteger(timeout)).toBe(true);
        expect(timeout).toBeGreaterThan(BACKEND_STATEMENT_TIMEOUT_MILLISECONDS);
    },
);

it.each(TIMEOUT_CASES)(
    "API_CLIENT $method sends the bound; its $code timeout rejects once, unretried, as the abend",
    async ({ method, code }) => {
        const adapter = jest.fn((config: InternalAxiosRequestConfig) =>
            Promise.reject(BuildTimeoutError(code, config)),
        );
        const timedOutCall = API_CLIENT.request({ url: ACCOUNT_URL, method, adapter });
        const rejection: unknown = await timedOutCall.then(
            () => undefined,
            (error: unknown) => error,
        );
        expect(adapter).toHaveBeenCalledTimes(1);
        await expect(adapter.mock.results[0]?.value).rejects.toBe(rejection);
        const sentTimeout = expect.objectContaining({
            timeout: AGREED_REQUEST_TIMEOUT_MILLISECONDS,
        });
        expect(adapter).toHaveBeenCalledWith(sentTimeout);
        ExpectNoRefusalEffects();
        expect(ToScreenFeedback(rejection)).toStrictEqual(ABEND_FEEDBACK);
        expect(IsCancelledRequest(rejection)).toBe(false);
    },
);

it.each(ORIGIN_CASES)("backend origin %p roots every call at one /api/v1", async (origin) => {
    const expectedRoot = BACKEND_ORIGIN + API_V1_PATH;
    await expect(LoadClientRoots(origin)).resolves.toStrictEqual([expectedRoot, expectedRoot]);
});

it.each(UNAUTHORIZED_CASES)(
    "401 from $url on $currentPath redirects to /signon: $navigates (CICS sign-on)",
    async ({ url, currentPath, navigates }) => {
        window.history.pushState({}, "", currentPath);
        await ExpectRejectedUnchanged(BuildAxiosError(401, {}, url));
        expect(NavigationWasAttempted(consoleErrorSpy)).toBe(navigates);
        expect(TakePendingMessage()).toBe("");
    },
);

it("API_CLIENT runs the 401 redirect on a rejected call (registered interceptor)", async () => {
    const accountCall = API_CLIENT.get(ACCOUNT_URL, {
        adapter: (config) => Promise.reject(BuildAxiosError(401, {}, config.url ?? "")),
    });
    await expect(accountCall).rejects.toBeInstanceOf(AxiosError);
    await expect(accountCall).rejects.toMatchObject({ response: { status: 401 } });
    expect(NavigationWasAttempted(consoleErrorSpy)).toBe(true);
});

it("API_CLIENT drops the previous user's home route on a successful login", async () => {
    SetHomeRoute(ADMIN_HOME_ROUTE);
    await API_CLIENT.post("/auth/login", undefined, {
        adapter: (config) => Promise.resolve(BuildAxiosResponse(200, {}, config)),
    });
    expect(GetHomeRoute()).toBe(DEFAULT_HOME_ROUTE);
    expect(window.sessionStorage.getItem(HOME_ROUTE_KEY)).toBeNull();
});

it.each(LOGIN_RESPONSE_CASES)(
    "$status $method $url clears the home route: $clears (sign-on replaces the COMMAREA)",
    ({ method, url, status, clears }) => {
        SetHomeRoute(ADMIN_HOME_ROUTE);
        const response = BuildAxiosResponse(status, {}, BuildRequestConfig(url, method));
        expect(HandleResponse(response)).toBe(response);
        expect(GetHomeRoute()).toBe(clears ? DEFAULT_HOME_ROUTE : ADMIN_HOME_ROUTE);
    },
);

it("403 stores the server message and returns to the home menu (COMMAREA XCTL back)", async () => {
    SetHomeRoute(DEFAULT_HOME_ROUTE);
    window.history.pushState({}, "", ADMIN_USERS_URL);
    const lookupSpy = MockHomeRouteLookup(DEFAULT_HOME_ROUTE);
    await ExpectRejectedUnchanged(BuildAxiosError(403, ADMIN_ONLY_BODY, ADMIN_USERS_URL));
    expect(NavigationWasAttempted(consoleErrorSpy)).toBe(true);
    expect(lookupSpy).toHaveBeenCalledTimes(1);
    // The refused page, still mounting while the browser leaves it, takes nothing.
    expect(TakePendingMessage()).toBe("");
    window.history.pushState({}, "", DEFAULT_HOME_ROUTE);
    expect(TakePendingMessage()).toBe(ADMIN_ONLY_MESSAGE);
    expect(TakePendingMessage()).toBe("");
});

it("403 with no message returns an admin to /admin storing nothing (COSGN00C type A)", async () => {
    SetHomeRoute(ADMIN_HOME_ROUTE);
    MockHomeRouteLookup(ADMIN_HOME_ROUTE);
    window.history.pushState({}, "", "/accounts/view");
    await ExpectRejectedUnchanged(BuildAxiosError(403, EMPTY_REFUSAL_BODY, ACCOUNT_URL));
    expect(GetHomeRoute()).toBe(ADMIN_HOME_ROUTE);
    expect(window.sessionStorage.getItem(PENDING_MESSAGE_KEY)).toBeNull();
    expect(NavigationWasAttempted(consoleErrorSpy)).toBe(true);
});

it.each(HOME_ROUTE_CASES)(
    "403 on the home menu %s itself stores and reloads nothing",
    async (home) => {
        SetHomeRoute(home);
        MockHomeRouteLookup(home);
        window.history.pushState({}, "", home);
        await ExpectRejectedUnchanged(BuildAxiosError(403, ADMIN_ONLY_BODY, ADMIN_USERS_URL));
        ExpectNoRefusalEffects();
    },
);

it.each(LOOKUP_CASES)(
    "403 under record $recordedRoute asks /auth/me (next_route $nextRoute, on $currentPath)",
    async ({ recordedRoute, nextRoute, currentPath, navigates, storedRoute }) => {
        RecordHomeRoute(recordedRoute);
        const lookupSpy = MockHomeRouteLookup(nextRoute);
        window.history.pushState({}, "", currentPath);
        await ExpectRejectedUnchanged(BuildAxiosError(403, ADMIN_ONLY_BODY, ADMIN_USERS_URL));
        expect(NavigationWasAttempted(consoleErrorSpy)).toBe(navigates);
        const pendingMessage = window.sessionStorage.getItem(PENDING_MESSAGE_KEY);
        expect(pendingMessage).toBe(navigates ? ADMIN_ONLY_MESSAGE : null);
        expect(window.sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(storedRoute);
        expect(lookupSpy).toHaveBeenCalledTimes(1);
        const lookupOptions = expect.objectContaining(EXPECTED_LOOKUP_OPTIONS);
        expect(lookupSpy).toHaveBeenCalledWith(CURRENT_USER_URL, lookupOptions);
    },
);

it("403 refusals arriving together share one /auth/me lookup", async () => {
    const lookupSpy = MockHomeRouteLookup(ADMIN_HOME_ROUTE);
    window.history.pushState({}, "", "/cards");
    const firstRefusal = BuildAxiosError(403, ADMIN_ONLY_BODY, ADMIN_USERS_URL);
    const secondRefusal = BuildAxiosError(403, ADMIN_ONLY_BODY, "/menu/admin");
    await Promise.all([
        ExpectRejectedUnchanged(firstRefusal),
        ExpectRejectedUnchanged(secondRefusal),
    ]);
    expect(lookupSpy).toHaveBeenCalledTimes(1);
    expect(GetHomeRoute()).toBe(ADMIN_HOME_ROUTE);
});

it.each(LOOKUP_FAILURE_CASES)(
    "403 whose lookup fails ($failure) falls back to record $recordedRoute (on $currentPath)",
    async ({ error, recordedRoute, currentPath, navigates }) => {
        RecordHomeRoute(recordedRoute);
        const lookupSpy = jest.spyOn(axios, "get").mockRejectedValue(error);
        window.history.pushState({}, "", currentPath);
        await ExpectRejectedUnchanged(BuildAxiosError(403, ADMIN_ONLY_BODY, ADMIN_USERS_URL));
        expect(NavigationWasAttempted(consoleErrorSpy)).toBe(navigates);
        expect(window.sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(recordedRoute);
        expect(lookupSpy).toHaveBeenCalledTimes(1);
        const lookupOptions = expect.objectContaining(EXPECTED_LOOKUP_OPTIONS);
        expect(lookupSpy).toHaveBeenCalledWith(CURRENT_USER_URL, lookupOptions);
    },
);

it("403 whose lookup fails with a non-axios defect rejects with that defect", async () => {
    const defect = new TypeError("home route lookup defect");
    jest.spyOn(axios, "get").mockRejectedValue(defect);
    window.history.pushState({}, "", "/cards");
    const refusal = BuildAxiosError(403, ADMIN_ONLY_BODY, ADMIN_USERS_URL);
    await expect(HandleResponseError(refusal)).rejects.toBe(defect);
    ExpectNoRefusalEffects();
});

it.each(SESSION_END_CASES)(
    "403 resolved after $ending neither stores nor navigates (stale COMMAREA)",
    async ({ EndSession }) => {
        const answerLookup = DeferHomeRouteLookup();
        window.history.pushState({}, "", "/cards");
        const refusal = BuildAxiosError(403, ADMIN_ONLY_BODY, ADMIN_USERS_URL);
        const handling = HandleResponseError(refusal);
        EndSession();
        answerLookup(ADMIN_HOME_ROUTE);
        await expect(handling).rejects.toBe(refusal);
        ExpectNoRefusalEffects();
        expect(GetHomeRoute()).toBe(DEFAULT_HOME_ROUTE);
    },
);

it.each(LEGACY_ERROR_CASES)(
    "$status is rejected unchanged without navigating or storing a message",
    async ({ status, body }) => {
        await ExpectRejectedUnchanged(BuildAxiosError(status, body, ACCOUNT_URL));
        ExpectNoRefusalEffects();
    },
);

it.each([...ABEND_CASES, ...STRUCTURAL_REJECTION_CASES])(
    "$label is rejected unchanged without navigating",
    async ({ error }) => {
        await ExpectRejectedUnchanged(error);
        ExpectNoRefusalEffects();
    },
);

it.each(LEGACY_ERROR_CASES)(
    "$status shows its legacy body verbatim on the message line: $body.message",
    ({ status, body }) => {
        expect(ToScreenFeedback(BuildAxiosError(status, body, ACCOUNT_URL))).toStrictEqual(body);
    },
);

it("absent feedback keys get their empty values and a null cursor is kept", () => {
    const sparseBody = { message: "Transaction ID NOT found..." };
    expect(ToScreenFeedback(BuildAxiosError(404, sparseBody, "/transactions/1"))).toStrictEqual({
        message: "Transaction ID NOT found...",
        info_message: "",
        field_errors: [],
        cursor_field: "",
    });
    const refusalFeedback = ToScreenFeedback(BuildAxiosError(403, ADMIN_ONLY_BODY, ACCOUNT_URL));
    expect(refusalFeedback.cursor_field).toBeNull();
});

it.each(ABEND_CASES)("$label becomes UNEXPECTED ABEND OCCURRED. (ABEND-ROUTINE)", ({ error }) => {
    expect(ToScreenFeedback(error)).toStrictEqual(ABEND_FEEDBACK);
    expect(LegacyErrorBody(error)).toBeNull();
});

it.each(STRUCTURAL_REJECTION_CASES)(
    "$label shows the abend text, marks $fieldErrors and puts the cursor on $cursorField",
    ({ error, fieldErrors, cursorField }) => {
        expect(ToScreenFeedback(error)).toStrictEqual({
            ...ABEND_FEEDBACK,
            field_errors: fieldErrors,
            cursor_field: cursorField,
        });
        expect(LegacyErrorBody(error)).toBeNull();
    },
);

it("a non-axios defect is rethrown instead of shown as a message", () => {
    const error = new Error("boom");
    expect(() => ToScreenFeedback(error)).toThrow(error);
    expect(() => LegacyErrorBody(error)).toThrow(error);
});

it.each(CANCELLATION_CASES)("$label is a cancellation: $isCancelled", ({ error, isCancelled }) => {
    expect(IsCancelledRequest(error)).toBe(isCancelled);
});

it.each(ABORT_TIMING_CASES)(
    "API_CLIENT rejects a read aborted $timing as a cancellation, with no refusal action",
    async ({ abortsInFlight }) => {
        window.history.pushState({}, "", "/cards");
        const controller = new AbortController();
        const adapter = jest.fn((config: InternalAxiosRequestConfig) => {
            controller.abort();
            return Promise.resolve(BuildAxiosResponse(200, {}, config));
        });
        if (!abortsInFlight) {
            controller.abort();
        }
        const read = API_CLIENT.get(ACCOUNT_URL, { adapter, signal: controller.signal });
        const rejection: unknown = await read.then(
            () => undefined,
            (error: unknown) => error,
        );
        expect(rejection).toBeInstanceOf(CanceledError);
        expect(IsCancelledRequest(rejection)).toBe(true);
        expect(adapter).toHaveBeenCalledTimes(abortsInFlight ? 1 : 0);
        ExpectNoRefusalEffects();
    },
);

it("LegacyErrorBody keeps the screen keys beside the completed feedback (CAUP COMMAREA)", () => {
    const screenBody = {
        message: "No change detected with respect to values fetched.",
        state: UpdateScreenState.SHOW_DETAILS,
        screen_token: "opaque-screen-state",
    };
    const legacyBody = LegacyErrorBody(BuildAxiosError(400, screenBody, ACCOUNT_URL));
    expect(legacyBody).toStrictEqual({
        ...screenBody,
        info_message: "",
        field_errors: [],
        cursor_field: "",
    });
});

it.each(SEVERITY_CASES)(
    "error $isError, completed write $isCompletedWrite shows $severity (ERRMSG colour)",
    ({ isError, isCompletedWrite, severity }) => {
        expect(SeverityForOutcome({ isError, isCompletedWrite })).toBe(severity);
    },
);

it("a pending message is delivered exactly once across a navigation", () => {
    StorePendingMessage("Unable to lookup User...");
    expect(window.sessionStorage.getItem(PENDING_MESSAGE_KEY)).toBe("Unable to lookup User...");
    expect(TakePendingMessage()).toBe("Unable to lookup User...");
    expect(TakePendingMessage()).toBe("");
});

// A message stored for one page reaches that page only; one stored without a route reaches the
// next page to ask. left is what stays stored after the page asks.
const PENDING_ROUTE_CASES: readonly {
    route: string | undefined;
    takenOn: string;
    taken: string;
    left: string | null;
}[] = [
    { route: DEFAULT_HOME_ROUTE, takenOn: ADMIN_USERS_URL, taken: "", left: ADMIN_ONLY_MESSAGE },
    {
        route: DEFAULT_HOME_ROUTE,
        takenOn: DEFAULT_HOME_ROUTE,
        taken: ADMIN_ONLY_MESSAGE,
        left: null,
    },
    { route: undefined, takenOn: ADMIN_USERS_URL, taken: ADMIN_ONLY_MESSAGE, left: null },
];

it.each(PENDING_ROUTE_CASES)(
    "a pending message for $route asked for on $takenOn gives $taken",
    ({ route, takenOn, taken, left }) => {
        StorePendingMessage(ADMIN_ONLY_MESSAGE, route);
        window.history.pushState({}, "", takenOn);
        expect(TakePendingMessage()).toBe(taken);
        expect(window.sessionStorage.getItem(PENDING_MESSAGE_KEY)).toBe(left);
    },
);

it("a pending message stored without a route replaces one kept for another page", () => {
    StorePendingMessage(ADMIN_ONLY_MESSAGE, DEFAULT_HOME_ROUTE);
    StorePendingMessage("Thank you for using CardDemo application...");
    window.history.pushState({}, "", SIGN_ON_ROUTE);
    expect(TakePendingMessage()).toBe("Thank you for using CardDemo application...");
    expect(window.sessionStorage.getItem(PENDING_MESSAGE_ROUTE_KEY)).toBeNull();
});

it("the home route defaults to the main menu, records /admin and clears on sign-off", () => {
    expect(GetHomeRoute()).toBe(DEFAULT_HOME_ROUTE);
    SetHomeRoute(ADMIN_HOME_ROUTE);
    expect(GetHomeRoute()).toBe(ADMIN_HOME_ROUTE);
    expect(window.sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(ADMIN_HOME_ROUTE);
    ClearHomeRoute();
    expect(GetHomeRoute()).toBe(DEFAULT_HOME_ROUTE);
    expect(window.sessionStorage.getItem(HOME_ROUTE_KEY)).toBeNull();
});

it("a home route recorded before a full page load is read back from sessionStorage", () => {
    window.sessionStorage.setItem(HOME_ROUTE_KEY, ADMIN_HOME_ROUTE);
    expect(GetHomeRoute()).toBe(ADMIN_HOME_ROUTE);
});

it.each(NON_HOME_ROUTES)("SetHomeRoute(%p) is refused and discards the earlier record", (route) => {
    SetHomeRoute(ADMIN_HOME_ROUTE);
    SetHomeRoute(route);
    expect(GetHomeRoute()).toBe(DEFAULT_HOME_ROUTE);
    expect(window.sessionStorage.getItem(HOME_ROUTE_KEY)).toBeNull();
});

it.each(NON_HOME_ROUTES)("a stored home route %p is removed and never used", (route) => {
    window.sessionStorage.setItem(HOME_ROUTE_KEY, route);
    expect(GetHomeRoute()).toBe(DEFAULT_HOME_ROUTE);
    expect(window.sessionStorage.getItem(HOME_ROUTE_KEY)).toBeNull();
});

// The admin tree is COADM01 and the COUSR00-COUSR03 pages under it, compared exactly; every other
// page but sign-on belongs to the main menu's tree.
const MENU_TREE_CASES: readonly { pathname: string; treeHomeRoute: string | null }[] = [
    { pathname: ADMIN_HOME_ROUTE, treeHomeRoute: ADMIN_HOME_ROUTE },
    { pathname: ADMIN_USERS_URL, treeHomeRoute: ADMIN_HOME_ROUTE },
    { pathname: "/admin/users/delete", treeHomeRoute: ADMIN_HOME_ROUTE },
    { pathname: "/administrator", treeHomeRoute: DEFAULT_HOME_ROUTE },
    { pathname: "/ADMIN", treeHomeRoute: DEFAULT_HOME_ROUTE },
    { pathname: DEFAULT_HOME_ROUTE, treeHomeRoute: DEFAULT_HOME_ROUTE },
    { pathname: "/accounts/view", treeHomeRoute: DEFAULT_HOME_ROUTE },
    { pathname: "/reports/transactions", treeHomeRoute: DEFAULT_HOME_ROUTE },
    { pathname: SIGN_ON_ROUTE, treeHomeRoute: null },
];

it.each(MENU_TREE_CASES)(
    "page $pathname belongs to the menu tree of $treeHomeRoute",
    ({ pathname, treeHomeRoute }) => {
        expect(MenuTreeHomeRoute(pathname)).toBe(treeHomeRoute);
    },
);

it.each(STORAGE_REFUSAL_NAMES)(
    "a refused sessionStorage (%s) leaves the in-memory state",
    (name) => {
        RefuseSessionStorage(new DOMException("storage refused", name));
        expect(() => StorePendingMessage(ADMIN_ONLY_MESSAGE)).not.toThrow();
        expect(TakePendingMessage()).toBe("");
        SetHomeRoute(ADMIN_HOME_ROUTE);
        expect(GetHomeRoute()).toBe(ADMIN_HOME_ROUTE);
        ClearHomeRoute();
        expect(GetHomeRoute()).toBe(DEFAULT_HOME_ROUTE);
    },
);

it("any other sessionStorage error is a defect and propagates", () => {
    const defect = new TypeError("storage defect");
    RefuseSessionStorage(defect);
    expect(() => StorePendingMessage(ADMIN_ONLY_MESSAGE)).toThrow(defect);
    expect(() => TakePendingMessage()).toThrow(defect);
});

// Allowances shaped like the pages' own: a menu's pathnames, and the CU00 selection pages with the
// userId that COUSR00C's urlencode puts on them (a blank becomes "+"). The user IDs are fictitious.
const MENU_ROUTE_ALLOWANCE: RouteAllowance = { pathnames: ["/accounts/view", "/billpay"] };
const UPDATE_ROUTE = "/admin/users/update";
const DELETE_ROUTE = "/admin/users/delete";
const SELECTION_ROUTE_ALLOWANCE: RouteAllowance = {
    pathnames: [UPDATE_ROUTE, DELETE_ROUTE],
    queryKey: "userId",
};
// Any origin reads the canonical route back the way the target page's useSearchParams does.
const READ_BACK_ORIGIN = "http://127.0.0.1";

interface AllowedRouteCase {
    route: string;
    allowance: RouteAllowance;
    canonicalRoute: string;
    userId: string | null;
}

const ALLOWED_ROUTE_CASES: readonly AllowedRouteCase[] = [
    { route: "/menu", allowance: HOME_ROUTE_ALLOWANCE, canonicalRoute: "/menu", userId: null },
    { route: "/admin", allowance: HOME_ROUTE_ALLOWANCE, canonicalRoute: "/admin", userId: null },
    {
        route: "/billpay",
        allowance: MENU_ROUTE_ALLOWANCE,
        canonicalRoute: "/billpay",
        userId: null,
    },
    {
        route: `${UPDATE_ROUTE}?userId=TESTUSR1`,
        allowance: SELECTION_ROUTE_ALLOWANCE,
        canonicalRoute: `${UPDATE_ROUTE}?userId=TESTUSR1`,
        userId: "TESTUSR1",
    },
    // Python's urlencode and URLSearchParams agree: "+" for a blank and %26 for "&".
    {
        route: `${DELETE_ROUTE}?userId=TEST+U%26R`,
        allowance: SELECTION_ROUTE_ALLOWANCE,
        canonicalRoute: `${DELETE_ROUTE}?userId=TEST+U%26R`,
        userId: "TEST U&R",
    },
    // Any other spelling of the same value is re-encoded, never passed on as the server wrote it.
    {
        route: `${UPDATE_ROUTE}?userId=A%20B&`,
        allowance: SELECTION_ROUTE_ALLOWANCE,
        canonicalRoute: `${UPDATE_ROUTE}?userId=A+B`,
        userId: "A B",
    },
    {
        route: `${UPDATE_ROUTE}?userId=A=B%26C`,
        allowance: SELECTION_ROUTE_ALLOWANCE,
        canonicalRoute: `${UPDATE_ROUTE}?userId=A%3DB%26C`,
        userId: "A=B&C",
    },
];

interface RefusedRouteCase {
    label: string;
    route: unknown;
    allowance: RouteAllowance;
}

function RefusedRoute(route: unknown, allowance = MENU_ROUTE_ALLOWANCE): RefusedRouteCase {
    return { label: String(JSON.stringify(route)), route, allowance };
}

const REFUSED_ROUTE_CASES: readonly RefusedRouteCase[] = [
    ...[null, undefined, 42, true, {}, ["/billpay"], ""].map((route) => RefusedRoute(route)),
    ...[
        "//evil.example",
        "//evil.example/billpay",
        "https://evil.example/billpay",
        "javascript:alert(1)",
        "data:text/html,<p>x</p>",
        "/\\evil.example",
        "/billpay\\..",
        "/billpay/",
        "/BILLPAY",
        " /billpay",
        "/billpay ",
        "/billpay\n",
        "/bill\tpay",
        "/billpay\u0000",
        "/accounts/../billpay",
        "/accounts/%2e%2e/billpay",
        "/billpay/.",
        "/billpay?x=1",
        "/billpay?",
        "/billpay#x",
        "/billpay?userId=TESTUSR1",
    ].map((route) => RefusedRoute(route)),
    ...[
        UPDATE_ROUTE,
        `${UPDATE_ROUTE}?`,
        `${UPDATE_ROUTE}?userId=A&userId=B`,
        `${UPDATE_ROUTE}?userId=`,
        `${UPDATE_ROUTE}?userId`,
        `${UPDATE_ROUTE}?userId=+`,
        `${UPDATE_ROUTE}?userId=%20%09`,
        `${UPDATE_ROUTE}?userId=A%00`,
        `${UPDATE_ROUTE}?other=1`,
        `${UPDATE_ROUTE}?userId=A&other=1`,
        `${UPDATE_ROUTE}?userId=A=B&C`,
        `${UPDATE_ROUTE}?userid=A`,
        `${UPDATE_ROUTE}?userId=A#x`,
        `${UPDATE_ROUTE}/?userId=A`,
        "/admin/users/view?userId=A",
        "//evil.example?userId=A",
    ].map((route) => RefusedRoute(route, SELECTION_ROUTE_ALLOWANCE)),
    ...[
        "//evil.example",
        "https://evil.example/menu",
        "javascript:alert(1)",
        "/menu/",
        "/MENU",
        "/menu?x=1",
        "/menu#x",
        " /menu",
        "/menu\\..",
    ].map((route) => RefusedRoute(route, HOME_ROUTE_ALLOWANCE)),
    // A malformed allowance still never yields another origin or a rewritten path.
    RefusedRoute("//evil.example", { pathnames: ["//evil.example"] }),
    RefusedRoute("https://evil.example", { pathnames: ["https://evil.example"] }),
    RefusedRoute("/a/../billpay", { pathnames: ["/a/../billpay"] }),
    RefusedRoute("http://[", { pathnames: ["http://["] }),
];

it.each(ALLOWED_ROUTE_CASES)(
    "ToAllowedRoute accepts $route as $canonicalRoute",
    ({ route, allowance, canonicalRoute, userId }) => {
        expect(ToAllowedRoute(route, allowance)).toBe(canonicalRoute);
        const readBack = new URL(canonicalRoute, READ_BACK_ORIGIN).searchParams.get("userId");
        expect(readBack).toBe(userId);
    },
);

it.each(REFUSED_ROUTE_CASES)("ToAllowedRoute refuses $label", ({ route, allowance }) => {
    expect(ToAllowedRoute(route, allowance)).toBeNull();
});

it.each(PATH_KEY_CASES)(
    'path key "$key" travels as the one segment $segment',
    ({ key, segment }) => {
        const pathKey = EncodePathKey(key);
        const requestUrl = new URL(`${TRANSACTIONS_URL}/${pathKey}`, BACKEND_ORIGIN);
        expect(pathKey).toBe(segment);
        expect(requestUrl.pathname).toBe(`${TRANSACTIONS_URL}/${segment}`);
        expect(decodeURIComponent(pathKey).trimEnd()).toBe(key.trimEnd());
    },
);

it.each(DOT_SEGMENT_CASES)(
    "an unpadded $segment segment would leave the detail route for $pathname",
    ({ segment, pathname }) => {
        expect(new URL(`${TRANSACTIONS_URL}/${segment}`, BACKEND_ORIGIN).pathname).toBe(pathname);
    },
);
