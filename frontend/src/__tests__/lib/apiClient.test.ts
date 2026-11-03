// Tests: lib/apiClient.ts (replaces the COCOM01Y COMMAREA hand-off, the COSGN00C XCTL to the
// user's own menu, the CSMSG01Y/CSMSG02Y message areas and the BMS ERRMSG line contract).
import axios, {
    AxiosError,
    AxiosHeaders,
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
    HOME_ROUTE_KEY,
    LegacyErrorBody,
    PENDING_MESSAGE_KEY,
    REQUEST_TIMEOUT_MILLISECONDS,
    SeverityForOutcome,
    SetHomeRoute,
    SIGN_ON_ROUTE,
    StorePendingMessage,
    TakePendingMessage,
    ToScreenFeedback,
    UpdateScreenState,
    type LegacyErrorResponse,
    type MessageSeverity,
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

// FastAPI's 422 body: the detail list beside empty legacy keys.
const VALIDATION_BODY = {
    ...BuildLegacyBody("", null),
    detail: [{ loc: ["body", "acct_id"], msg: "String too long", type: "string_too_long" }],
};

const ABEND_CASES: readonly { label: string; error: AxiosError }[] = [
    {
        label: "500 with a legacy body",
        error: BuildAxiosError(500, BuildLegacyBody("Update of record failed", null), ACCOUNT_URL),
    },
    { label: "502 proxy page", error: BuildAxiosError(502, "<html>Bad Gateway</html>", "/cards") },
    { label: "422 detail list", error: BuildAxiosError(422, VALIDATION_BODY, "/transactions") },
    { label: "network failure", error: BuildNetworkError("/billpay") },
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

// The /auth/me next_route decides the target: a refusal on that route itself does not reload.
const LOOKUP_CASES: readonly {
    nextRoute: unknown;
    currentPath: string;
    navigates: boolean;
    storedRoute: string | null;
}[] = [
    {
        nextRoute: ADMIN_HOME_ROUTE,
        currentPath: "/cards",
        navigates: true,
        storedRoute: ADMIN_HOME_ROUTE,
    },
    {
        nextRoute: ADMIN_HOME_ROUTE,
        currentPath: ADMIN_HOME_ROUTE,
        navigates: false,
        storedRoute: ADMIN_HOME_ROUTE,
    },
    {
        nextRoute: DEFAULT_HOME_ROUTE,
        currentPath: ADMIN_USERS_URL,
        navigates: true,
        storedRoute: DEFAULT_HOME_ROUTE,
    },
    {
        nextRoute: ADMIN_USERS_URL,
        currentPath: DEFAULT_HOME_ROUTE,
        navigates: false,
        storedRoute: null,
    },
    { nextRoute: undefined, currentPath: "/cards", navigates: true, storedRoute: null },
];

const LOOKUP_FAILURE_CASES: readonly { currentPath: string; navigates: boolean }[] = [
    { currentPath: "/cards", navigates: true },
    { currentPath: DEFAULT_HOME_ROUTE, navigates: false },
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

function ExpectNoRefusalEffects(): void {
    expect(NavigationWasAttempted(consoleErrorSpy)).toBe(false);
    expect(TakePendingMessage()).toBe("");
}

function BuildLookupAnswer(nextRoute: unknown): AxiosResponse {
    return BuildAxiosResponse(200, { next_route: nextRoute }, BuildRequestConfig(CURRENT_USER_URL));
}

function MockHomeRouteLookup(nextRoute: unknown): jest.SpyInstance {
    return jest.spyOn(axios, "get").mockResolvedValue(BuildLookupAnswer(nextRoute));
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
    expect(API_CLIENT.defaults.timeout).toBe(REQUEST_TIMEOUT_MILLISECONDS);
});

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
    const lookupSpy = jest.spyOn(axios, "get");
    await ExpectRejectedUnchanged(BuildAxiosError(403, ADMIN_ONLY_BODY, ADMIN_USERS_URL));
    expect(TakePendingMessage()).toBe(ADMIN_ONLY_MESSAGE);
    expect(TakePendingMessage()).toBe("");
    expect(NavigationWasAttempted(consoleErrorSpy)).toBe(true);
    expect(lookupSpy).not.toHaveBeenCalled();
});

it("403 with no message returns an admin to /admin storing nothing (COSGN00C type A)", async () => {
    SetHomeRoute(ADMIN_HOME_ROUTE);
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
        window.history.pushState({}, "", home);
        await ExpectRejectedUnchanged(BuildAxiosError(403, ADMIN_ONLY_BODY, ADMIN_USERS_URL));
        ExpectNoRefusalEffects();
    },
);

it.each(LOOKUP_CASES)(
    "403 with no recorded home asks /auth/me (next_route $nextRoute, on $currentPath)",
    async ({ nextRoute, currentPath, navigates, storedRoute }) => {
        const lookupSpy = MockHomeRouteLookup(nextRoute);
        window.history.pushState({}, "", currentPath);
        await ExpectRejectedUnchanged(BuildAxiosError(403, ADMIN_ONLY_BODY, ADMIN_USERS_URL));
        expect(NavigationWasAttempted(consoleErrorSpy)).toBe(navigates);
        expect(TakePendingMessage()).toBe(navigates ? ADMIN_ONLY_MESSAGE : "");
        expect(window.sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(storedRoute);
        expect(lookupSpy).toHaveBeenCalledTimes(1);
        const lookupOptions = expect.objectContaining({ withCredentials: true });
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
    "403 whose /auth/me lookup fails returns to the main menu (on $currentPath)",
    async ({ currentPath, navigates }) => {
        jest.spyOn(axios, "get").mockRejectedValue(BuildNetworkError(CURRENT_USER_URL));
        window.history.pushState({}, "", currentPath);
        await ExpectRejectedUnchanged(BuildAxiosError(403, ADMIN_ONLY_BODY, ADMIN_USERS_URL));
        expect(NavigationWasAttempted(consoleErrorSpy)).toBe(navigates);
        expect(window.sessionStorage.getItem(HOME_ROUTE_KEY)).toBeNull();
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

it.each(ABEND_CASES)("$label is rejected unchanged without navigating", async ({ error }) => {
    await ExpectRejectedUnchanged(error);
    ExpectNoRefusalEffects();
});

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

it("a non-axios defect is rethrown instead of shown as a message", () => {
    const error = new Error("boom");
    expect(() => ToScreenFeedback(error)).toThrow(error);
    expect(() => LegacyErrorBody(error)).toThrow(error);
});

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
