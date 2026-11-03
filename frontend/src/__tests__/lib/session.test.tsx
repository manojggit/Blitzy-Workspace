// Tests: lib/session.tsx (replaces COCOM01Y CDEMO-USER-ID / CDEMO-USER-TYPE, which COSGN00C
// filled from USRSEC before its XCTL to COADM01C for type A or to COMEN01C otherwise).
import { Stack, Typography } from "@mui/material";
import { act, render, screen, waitFor, type RenderResult } from "@testing-library/react";
import axios, {
    AxiosError,
    AxiosHeaders,
    CanceledError,
    type AxiosResponse,
    type InternalAxiosRequestConfig,
} from "axios";
import { StrictMode, useEffect, type ReactElement } from "react";

import { FetchCurrentUser, type CurrentUserResponse } from "@/lib/api/auth";
import { FetchAdminMenu, FetchMainMenu, type MenuResponse } from "@/lib/api/menu";
import type * as ApiClientModule from "@/lib/apiClient";
import {
    ClearHomeRoute,
    HandleResponseError,
    HOME_ROUTE_KEY,
    PENDING_MESSAGE_KEY,
    PENDING_MESSAGE_ROUTE_KEY,
    RedirectToSignOn,
    SetHomeRoute,
    SIGN_ON_ROUTE,
    type ReadOptions,
} from "@/lib/apiClient";
import { SessionProvider, useSession, type SessionContextValue } from "@/lib/session";

jest.mock("@/lib/api/auth");
// The page-tree menu reads the provider sends; each test decides how the server answers them.
jest.mock("@/lib/api/menu");

// The real home-route bookkeeping and sign-on navigation still run; the wrappers only record the
// calls session.tsx makes.
jest.mock("@/lib/apiClient", () => {
    const actual = jest.requireActual<typeof ApiClientModule>("@/lib/apiClient");
    return {
        ...actual,
        SetHomeRoute: jest.fn(actual.SetHomeRoute),
        ClearHomeRoute: jest.fn(actual.ClearHomeRoute),
        RedirectToSignOn: jest.fn(actual.RedirectToSignOn),
    };
});

const fetchCurrentUserMock = jest.mocked(FetchCurrentUser);
const setHomeRouteMock = jest.mocked(SetHomeRoute);
const clearHomeRouteMock = jest.mocked(ClearHomeRoute);
const redirectToSignOnMock = jest.mocked(RedirectToSignOn);
const fetchMainMenuMock = jest.mocked(FetchMainMenu);
const fetchAdminMenuMock = jest.mocked(FetchAdminMenu);

const MAIN_MENU_ROUTE = "/menu";
const ADMIN_MENU_ROUTE = "/admin";
const CURRENT_USER_PATH = "/auth/me";
const HTTP_UNAUTHORIZED = 401;
const HTTP_FORBIDDEN = 403;
const HTTP_NOT_FOUND = 404;
const HTTP_INTERNAL_SERVER_ERROR = 500;
const HTTP_SERVICE_UNAVAILABLE = 503;

// jsdom implements no navigation: every window.location.assign is reported through
// console.error, because window.location cannot be mocked under jsdom 26.
const NAVIGATION_NOT_IMPLEMENTED = "Not implemented: navigation";

// Eight characters, the CDEMO-USER-ID PIC X(08) width.
const TEST_USER_ID = "TESTUSR1";
const TEST_ADMIN_ID = "TESTADM1";

const REGULAR_USER: CurrentUserResponse = {
    user_id: TEST_USER_ID,
    user_type: "U",
    first_name: "TEST",
    last_name: "USER",
    next_route: MAIN_MENU_ROUTE,
};

const ADMIN_USER: CurrentUserResponse = {
    user_id: TEST_ADMIN_ID,
    user_type: "A",
    first_name: "TEST",
    last_name: "ADMIN",
    next_route: ADMIN_MENU_ROUTE,
};

const SESSION_CONTEXT_KEYS = ["ClearSession", "RefreshSession", "currentUser", "isLoading"];
const TAMPERED_USER_TYPE_KEY = "user_type";

const LOADING_TEST_ID = "session-loading";
const USER_ID_TEST_ID = "session-user-id";
const USER_TYPE_TEST_ID = "session-user-type";
const NO_USER_SHOWN = "none";

interface SessionConsumerProps {
    onValue?: (value: SessionContextValue) => void;
}

// One way the /auth/me read in flight is abandoned, and the session and home route it leaves.
interface AbandonedReadCase {
    caseName: string;
    Abandon: (session: SessionContextValue, view: RenderResult) => void;
    expectedUser: CurrentUserResponse | null;
    expectedIsLoading: boolean;
    expectedHomeRoute: string | null;
}

interface RefreshInFlight {
    view: RenderResult;
    session: SessionContextValue;
    abandonedRefresh: Promise<CurrentUserResponse | null>;
}

interface MountReadCase {
    path: string;
    reads: number;
    expectedUser: CurrentUserResponse | null;
}

// The page the provider mounts on, with /auth/me answering REGULAR_USER. COSGN00C reads USRSEC
// only once ENTER is pressed, so the sign-on page has no user to read before login; every other
// page reads /auth/me at once.
const MOUNT_READ_CASES: readonly MountReadCase[] = [
    { path: SIGN_ON_ROUTE, reads: 0, expectedUser: null },
    { path: MAIN_MENU_ROUTE, reads: 1, expectedUser: REGULAR_USER },
];

// How every /auth/me read fails, on the page the provider mounts on, and the sign-on navigation
// that failure leads to.
interface MountFailureCase {
    caseName: string;
    path: string;
    error: Error;
    reads: number;
    redirects: number;
}

interface RefreshFailureCase {
    caseName: string;
    path: string;
    error: AxiosError;
    reads: number;
}

const NETWORK_ERROR = BuildNoResponseError(AxiosError.ERR_NETWORK);

// Off the sign-on page, a first read that nothing else answers (no answer, a timeout, a 5xx or
// another status) goes to sign-on once, and is never retried. A 401 or 403 is the apiClient
// interceptor's to route, and a cancellation is discarded. The sign-on page reads nothing, so it
// never redirects to itself, even with the backend unreachable.
const MOUNT_FAILURE_CASES: readonly MountFailureCase[] = [
    {
        caseName: "no answer (ERR_NETWORK)",
        path: MAIN_MENU_ROUTE,
        error: NETWORK_ERROR,
        reads: 1,
        redirects: 1,
    },
    {
        caseName: "a timeout (ECONNABORTED)",
        path: MAIN_MENU_ROUTE,
        error: BuildNoResponseError(AxiosError.ECONNABORTED),
        reads: 1,
        redirects: 1,
    },
    {
        caseName: "a timeout (ETIMEDOUT)",
        path: MAIN_MENU_ROUTE,
        error: BuildNoResponseError(AxiosError.ETIMEDOUT),
        reads: 1,
        redirects: 1,
    },
    {
        caseName: "a 500",
        path: MAIN_MENU_ROUTE,
        error: BuildResponseError(HTTP_INTERNAL_SERVER_ERROR),
        reads: 1,
        redirects: 1,
    },
    {
        caseName: "a 503",
        path: MAIN_MENU_ROUTE,
        error: BuildResponseError(HTTP_SERVICE_UNAVAILABLE),
        reads: 1,
        redirects: 1,
    },
    {
        caseName: "a 404",
        path: MAIN_MENU_ROUTE,
        error: BuildResponseError(HTTP_NOT_FOUND),
        reads: 1,
        redirects: 1,
    },
    {
        caseName: "a 401",
        path: MAIN_MENU_ROUTE,
        error: BuildResponseError(HTTP_UNAUTHORIZED),
        reads: 1,
        redirects: 0,
    },
    {
        caseName: "a 403",
        path: MAIN_MENU_ROUTE,
        error: BuildResponseError(HTTP_FORBIDDEN),
        reads: 1,
        redirects: 0,
    },
    {
        caseName: "a cancellation",
        path: MAIN_MENU_ROUTE,
        error: new CanceledError(),
        reads: 1,
        redirects: 0,
    },
    { caseName: "no answer", path: SIGN_ON_ROUTE, error: NETWORK_ERROR, reads: 0, redirects: 0 },
];

// RefreshSession reports a missing user to its caller and never navigates, whether it reads the
// user after a login on the sign-on page or re-reads it on any other page.
const REFRESH_FAILURE_CASES: readonly RefreshFailureCase[] = [
    { caseName: "no answer", path: SIGN_ON_ROUTE, error: NETWORK_ERROR, reads: 1 },
    {
        caseName: "a 503",
        path: SIGN_ON_ROUTE,
        error: BuildResponseError(HTTP_SERVICE_UNAVAILABLE),
        reads: 1,
    },
    { caseName: "no answer", path: MAIN_MENU_ROUTE, error: NETWORK_ERROR, reads: 2 },
    {
        caseName: "a 500",
        path: MAIN_MENU_ROUTE,
        error: BuildResponseError(HTTP_INTERNAL_SERVER_ERROR),
        reads: 2,
    },
];

// jsdom's starting URL, restored after a test moves to another page.
const jsdomStartUrl = window.location.href;
// The console.error each test's spy forwards every report but jsdom's navigation reports to.
const unpatchedConsoleError = console.error.bind(console);

let capturedSession: SessionContextValue | null = null;
let consoleErrorSpy: jest.SpyInstance;

function BuildCurrentUserConfig(): InternalAxiosRequestConfig {
    return { url: CURRENT_USER_PATH, headers: new AxiosHeaders() };
}

// An /auth/me answer with an error status, coded as axios's settle codes it: a bad request below
// 500, a bad response from 500 on.
function BuildResponseError(status: number): AxiosError {
    const config = BuildCurrentUserConfig();
    const response = {
        status,
        data: { message: "", info_message: "", field_errors: [], cursor_field: null },
        statusText: "",
        headers: {},
        config,
    };
    const code =
        status < HTTP_INTERNAL_SERVER_ERROR
            ? AxiosError.ERR_BAD_REQUEST
            : AxiosError.ERR_BAD_RESPONSE;
    return new AxiosError("Request failed", code, config, undefined, response);
}

// An /auth/me call that got no answer at all: a network failure (ERR_NETWORK) or a timeout
// (ECONNABORTED, or ETIMEDOUT under axios's clarifyTimeoutError).
function BuildNoResponseError(code: string): AxiosError {
    return new AxiosError("Request failed", code, BuildCurrentUserConfig());
}

function SessionConsumer({ onValue }: SessionConsumerProps): ReactElement {
    const session = useSession();
    useEffect(() => {
        onValue?.(session);
    }, [onValue, session]);
    return (
        <Stack>
            <Typography data-testid={LOADING_TEST_ID}>{String(session.isLoading)}</Typography>
            <Typography data-testid={USER_ID_TEST_ID}>
                {session.currentUser?.user_id ?? NO_USER_SHOWN}
            </Typography>
            <Typography data-testid={USER_TYPE_TEST_ID}>
                {session.currentUser?.user_type ?? ""}
            </Typography>
        </Stack>
    );
}

// A page that re-reads the session from its own mount effect, which React runs before the
// provider's.
function RefreshOnMount(): ReactElement {
    const { RefreshSession } = useSession();
    useEffect(() => {
        void RefreshSession();
    }, [RefreshSession]);
    return <SessionConsumer />;
}

function RenderSessionConsumer(onValue?: (value: SessionContextValue) => void): RenderResult {
    return render(
        <SessionProvider>
            <SessionConsumer onValue={onValue} />
        </SessionProvider>,
    );
}

function CaptureSession(value: SessionContextValue): void {
    capturedSession = value;
}

function LatestSession(): SessionContextValue {
    if (capturedSession === null) {
        throw new Error("SessionConsumer has not rendered a session value");
    }
    return capturedSession;
}

function ShownValue(testId: string): string | null {
    return screen.getByTestId(testId).textContent;
}

async function WaitForSessionLoaded(): Promise<void> {
    await waitFor(() => {
        expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    });
}

// The resolver of a /auth/me read that stays pending until the test settles it.
function DeferCurrentUser(): (user: CurrentUserResponse) => void {
    let resolveUser: (user: CurrentUserResponse) => void = () => undefined;
    fetchCurrentUserMock.mockReturnValue(
        new Promise<CurrentUserResponse>((resolve) => {
            resolveUser = resolve;
        }),
    );
    return (user) => {
        resolveUser(user);
    };
}

// The rejecter of the first /auth/me read, which stays pending until the test fails it, whatever
// its signal; every later read answers REGULAR_USER.
function DeferFirstReadFailure(): (error: AxiosError) => void {
    let rejectUser: (error: AxiosError) => void = () => undefined;
    const pendingRead = new Promise<CurrentUserResponse>((_resolve, reject) => {
        rejectUser = reject;
    });
    fetchCurrentUserMock.mockReturnValueOnce(pendingRead).mockResolvedValue(REGULAR_USER);
    return (error) => {
        rejectUser(error);
    };
}

function IsNavigationReport(report: unknown): boolean {
    const text = report instanceof Error ? report.message : String(report);
    return text.includes(NAVIGATION_NOT_IMPLEMENTED);
}

// jsdom's navigation reports are counted, not printed. Every other report still prints, so an
// act() warning stays visible.
function ReportUnlessNavigation(...reports: unknown[]): void {
    if (!IsNavigationReport(reports[0])) {
        unpatchedConsoleError(...reports);
    }
}

// The window.location.assign calls jsdom reported during this test.
function NavigationAttempts(): number {
    const reports: unknown[][] = consoleErrorSpy.mock.calls;
    return reports.filter((report) => IsNavigationReport(report[0])).length;
}

async function RenderSignedOnSession(user: CurrentUserResponse): Promise<SessionContextValue> {
    fetchCurrentUserMock.mockResolvedValue(user);
    RenderSessionConsumer(CaptureSession);
    await WaitForSessionLoaded();
    return LatestSession();
}

// A /auth/me read that stays in flight until its signal is aborted, then rejects as axios does.
function RejectOnAbort(options?: ReadOptions): Promise<CurrentUserResponse> {
    return new Promise((_resolve, reject) => {
        options?.signal?.addEventListener("abort", () => {
            reject(new CanceledError());
        });
    });
}

// A /auth/me read answered as axios answers it: one whose signal is already aborted is cancelled
// unsent, any other gets the user. abortedAtSend records each call's signal state when sent.
function AnswerUnlessAborted(
    user: CurrentUserResponse,
    abortedAtSend: (boolean | undefined)[],
): (options?: ReadOptions) => Promise<CurrentUserResponse> {
    return (options) => {
        abortedAtSend.push(options?.signal?.aborted);
        if (options?.signal?.aborted) {
            return Promise.reject(new CanceledError());
        }
        return Promise.resolve(user);
    };
}

// The AbortSignal the session gave one FetchCurrentUser call (0 = the first).
function ReadSignal(callIndex: number): AbortSignal {
    const signal = fetchCurrentUserMock.mock.calls[callIndex]?.[0]?.signal;
    if (signal === undefined) {
        throw new Error(`FetchCurrentUser call ${callIndex} carried no AbortSignal`);
    }
    return signal;
}

// Starts a RefreshSession inside act and returns its result, still pending.
function StartRefresh(session: SessionContextValue): Promise<CurrentUserResponse | null> {
    let refresh: Promise<CurrentUserResponse | null> = Promise.resolve(null);
    act(() => {
        refresh = session.RefreshSession();
    });
    return refresh;
}

// A signed-on session whose RefreshSession read stays in flight until its signal is aborted.
async function RenderRefreshInFlight(): Promise<RefreshInFlight> {
    fetchCurrentUserMock.mockResolvedValueOnce(REGULAR_USER).mockImplementation(RejectOnAbort);
    const view = RenderSessionConsumer(CaptureSession);
    await WaitForSessionLoaded();
    const session = LatestSession();
    return { view, session, abandonedRefresh: StartRefresh(session) };
}

// The newer read stays in flight, so it alone owns the loading state and the user shown.
const ABANDONED_READ_CASES: readonly AbandonedReadCase[] = [
    {
        caseName: "a newer RefreshSession",
        Abandon: (session) => {
            void session.RefreshSession();
        },
        expectedUser: REGULAR_USER,
        expectedIsLoading: true,
        expectedHomeRoute: MAIN_MENU_ROUTE,
    },
    {
        caseName: "ClearSession (F3 sign-off)",
        Abandon: (session) => {
            session.ClearSession();
        },
        expectedUser: null,
        expectedIsLoading: false,
        expectedHomeRoute: null,
    },
    {
        caseName: "unmounting the provider",
        Abandon: (_session, view) => {
            view.unmount();
        },
        expectedUser: REGULAR_USER,
        expectedIsLoading: true,
        expectedHomeRoute: MAIN_MENU_ROUTE,
    },
];

// The first read fails with no answer only after it was abandoned, so it changes nothing: the
// read that replaced it, if any, alone decides the session.
const ABANDONED_FIRST_READ_CASES: readonly AbandonedReadCase[] = [
    {
        caseName: "a newer RefreshSession",
        Abandon: (session) => {
            void session.RefreshSession();
        },
        expectedUser: REGULAR_USER,
        expectedIsLoading: false,
        expectedHomeRoute: MAIN_MENU_ROUTE,
    },
    {
        caseName: "ClearSession (F3 sign-off)",
        Abandon: (session) => {
            session.ClearSession();
        },
        expectedUser: null,
        expectedIsLoading: false,
        expectedHomeRoute: null,
    },
    {
        caseName: "unmounting the provider",
        Abandon: (_session, view) => {
            view.unmount();
        },
        expectedUser: null,
        expectedIsLoading: true,
        expectedHomeRoute: null,
    },
];

beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    capturedSession = null;
    consoleErrorSpy = jest.spyOn(console, "error").mockImplementation(ReportUnlessNavigation);
});

afterEach(() => {
    consoleErrorSpy.mockRestore();
    window.history.replaceState(null, "", jsdomStartUrl);
});

it("shows no user while /auth/me is pending, then the user it returns", async () => {
    const resolveUser = DeferCurrentUser();
    RenderSessionConsumer();
    expect(ShownValue(LOADING_TEST_ID)).toBe("true");
    expect(ShownValue(USER_ID_TEST_ID)).toBe(NO_USER_SHOWN);
    expect(setHomeRouteMock).not.toHaveBeenCalled();
    await act(async () => {
        resolveUser(REGULAR_USER);
    });
    expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_USER_ID);
});

it("exposes the /auth/me user and sets the home menu (COCOM01Y CDEMO-USER-ID)", async () => {
    fetchCurrentUserMock.mockResolvedValue(REGULAR_USER);
    RenderSessionConsumer();
    await WaitForSessionLoaded();
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_USER_ID);
    expect(ShownValue(USER_TYPE_TEST_ID)).toBe("U");
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(1);
    expect(setHomeRouteMock).toHaveBeenCalledTimes(1);
    expect(setHomeRouteMock).toHaveBeenCalledWith(MAIN_MENU_ROUTE);
    expect(sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(MAIN_MENU_ROUTE);
});

it("records /admin for type A (COSGN00C CDEMO-USRTYP-ADMIN, XCTL to COADM01C)", async () => {
    await RenderSignedOnSession(ADMIN_USER);
    expect(ShownValue(USER_TYPE_TEST_ID)).toBe("A");
    expect(setHomeRouteMock).toHaveBeenCalledTimes(1);
    expect(setHomeRouteMock).toHaveBeenCalledWith(ADMIN_MENU_ROUTE);
    expect(sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(ADMIN_MENU_ROUTE);
});

it("401 from /auth/me leaves no current user (no COMMAREA, EIBCALEN = 0)", async () => {
    fetchCurrentUserMock.mockRejectedValue(BuildResponseError(HTTP_UNAUTHORIZED));
    RenderSessionConsumer(CaptureSession);
    await WaitForSessionLoaded();
    expect(ShownValue(USER_ID_TEST_ID)).toBe(NO_USER_SHOWN);
    expect(LatestSession().currentUser).toBeNull();
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(1);
    expect(setHomeRouteMock).not.toHaveBeenCalled();
});

it("RefreshSession re-reads /auth/me, as COSGN00C refilled the COMMAREA", async () => {
    const session = await RenderSignedOnSession(REGULAR_USER);
    fetchCurrentUserMock.mockResolvedValueOnce(ADMIN_USER);
    let refreshedUser: CurrentUserResponse | null = null;
    await act(async () => {
        refreshedUser = await session.RefreshSession();
    });
    expect(refreshedUser).toEqual(ADMIN_USER);
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(2);
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_ADMIN_ID);
    expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    expect(setHomeRouteMock).toHaveBeenLastCalledWith(ADMIN_MENU_ROUTE);
});

it("RefreshSession rejects a non-axios defect and keeps the user shown", async () => {
    const session = await RenderSignedOnSession(REGULAR_USER);
    const defect = new TypeError("defect in the /auth/me read");
    fetchCurrentUserMock.mockRejectedValueOnce(defect);
    await act(async () => {
        await expect(session.RefreshSession()).rejects.toBe(defect);
    });
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_USER_ID);
    expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    expect(setHomeRouteMock).toHaveBeenCalledTimes(1);
});

it("ClearSession drops the user and the home route (F3 sign-off)", async () => {
    const session = await RenderSignedOnSession(REGULAR_USER);
    act(() => {
        session.ClearSession();
    });
    expect(ShownValue(USER_ID_TEST_ID)).toBe(NO_USER_SHOWN);
    expect(LatestSession().currentUser).toBeNull();
    expect(LatestSession().isLoading).toBe(false);
    expect(clearHomeRouteMock).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(HOME_ROUTE_KEY)).toBeNull();
});

it("an /auth/me answer arriving after ClearSession does not undo the sign-off", async () => {
    const resolveUser = DeferCurrentUser();
    RenderSessionConsumer(CaptureSession);
    act(() => {
        LatestSession().ClearSession();
    });
    await act(async () => {
        resolveUser(REGULAR_USER);
    });
    expect(ShownValue(USER_ID_TEST_ID)).toBe(NO_USER_SHOWN);
    expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    expect(setHomeRouteMock).not.toHaveBeenCalled();
});

it("holds no client-side role: user type is USRSEC data only (COSGN00C SEC-USR-TYPE)", async () => {
    const session = await RenderSignedOnSession(ADMIN_USER);
    expect(Object.keys(session).sort()).toEqual(SESSION_CONTEXT_KEYS);
    expect(session).not.toHaveProperty("role");
    expect(session).not.toHaveProperty("isAdmin");
    expect(session.currentUser).toEqual(ADMIN_USER);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(1);
    expect(sessionStorage.key(0)).toBe(HOME_ROUTE_KEY);
});

it("stored values never change the server's user type (USRSEC, not the terminal)", async () => {
    sessionStorage.setItem(TAMPERED_USER_TYPE_KEY, "A");
    localStorage.setItem(TAMPERED_USER_TYPE_KEY, "A");
    sessionStorage.setItem(HOME_ROUTE_KEY, ADMIN_MENU_ROUTE);
    await RenderSignedOnSession(REGULAR_USER);
    expect(ShownValue(USER_TYPE_TEST_ID)).toBe("U");
    expect(setHomeRouteMock).toHaveBeenCalledWith(MAIN_MENU_ROUTE);
    expect(sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(MAIN_MENU_ROUTE);
});

it.each(ABANDONED_READ_CASES)(
    "$caseName aborts the /auth/me read in flight, whose cancellation changes nothing",
    async ({ Abandon, expectedUser, expectedIsLoading, expectedHomeRoute }) => {
        const { view, session, abandonedRefresh } = await RenderRefreshInFlight();
        expect(fetchCurrentUserMock).toHaveBeenNthCalledWith(2, {
            signal: expect.any(AbortSignal),
        });
        expect(ReadSignal(1).aborted).toBe(false);
        act(() => {
            Abandon(session, view);
        });
        expect(ReadSignal(1).aborted).toBe(true);
        await act(async () => {
            await expect(abandonedRefresh).resolves.toBeNull();
        });
        expect(LatestSession().currentUser).toEqual(expectedUser);
        expect(LatestSession().isLoading).toBe(expectedIsLoading);
        expect(setHomeRouteMock).toHaveBeenCalledTimes(1);
        expect(sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(expectedHomeRoute);
    },
);

it("StrictMode's effect re-run reads /auth/me again once the first read is aborted", async () => {
    fetchCurrentUserMock.mockImplementationOnce(RejectOnAbort).mockResolvedValue(REGULAR_USER);
    render(
        <StrictMode>
            <SessionProvider>
                <SessionConsumer />
            </SessionProvider>
        </StrictMode>,
    );
    await WaitForSessionLoaded();
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(2);
    expect(ReadSignal(0).aborted).toBe(true);
    expect(ReadSignal(1).aborted).toBe(false);
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_USER_ID);
    expect(setHomeRouteMock).toHaveBeenCalledTimes(1);
    expect(setHomeRouteMock).toHaveBeenCalledWith(MAIN_MENU_ROUTE);
});

it("a RefreshSession begun after the provider is disposed is cancelled unsent", async () => {
    const abortedAtSend: (boolean | undefined)[] = [];
    fetchCurrentUserMock.mockImplementation(AnswerUnlessAborted(REGULAR_USER, abortedAtSend));
    const view = RenderSessionConsumer(CaptureSession);
    await WaitForSessionLoaded();
    view.unmount();
    await act(async () => {
        await expect(LatestSession().RefreshSession()).resolves.toBeNull();
    });
    expect(abortedAtSend).toEqual([false, true]);
    expect(setHomeRouteMock).toHaveBeenCalledTimes(1);
    expect(clearHomeRouteMock).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(MAIN_MENU_ROUTE);
});

it("a RefreshSession a page begins in its first mount effect is sent live", async () => {
    const abortedAtSend: (boolean | undefined)[] = [];
    fetchCurrentUserMock.mockImplementation(AnswerUnlessAborted(REGULAR_USER, abortedAtSend));
    render(
        <SessionProvider>
            <RefreshOnMount />
        </SessionProvider>,
    );
    await WaitForSessionLoaded();
    // Both reads are sent live; the provider's own first read, begun after the page's, replaces it.
    expect(abortedAtSend).toEqual([false, false]);
    expect(ReadSignal(0).aborted).toBe(true);
    expect(ReadSignal(1).aborted).toBe(false);
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_USER_ID);
    expect(setHomeRouteMock).toHaveBeenCalledTimes(1);
    expect(setHomeRouteMock).toHaveBeenCalledWith(MAIN_MENU_ROUTE);
});

it.each(MOUNT_READ_CASES)(
    "mounted on $path reads /auth/me $reads time(s) and ends loading",
    async ({ path, reads, expectedUser }) => {
        window.history.replaceState(null, "", path);
        fetchCurrentUserMock.mockResolvedValue(REGULAR_USER);
        RenderSessionConsumer(CaptureSession);
        await WaitForSessionLoaded();
        expect(fetchCurrentUserMock).toHaveBeenCalledTimes(reads);
        expect(setHomeRouteMock).toHaveBeenCalledTimes(reads);
        expect(LatestSession().currentUser).toEqual(expectedUser);
        expect(LatestSession().isLoading).toBe(false);
    },
);

it("on the sign-on page RefreshSession after login reads /auth/me once", async () => {
    window.history.replaceState(null, "", SIGN_ON_ROUTE);
    fetchCurrentUserMock.mockResolvedValue(ADMIN_USER);
    RenderSessionConsumer(CaptureSession);
    await WaitForSessionLoaded();
    let refreshedUser: CurrentUserResponse | null = null;
    await act(async () => {
        refreshedUser = await LatestSession().RefreshSession();
    });
    expect(refreshedUser).toEqual(ADMIN_USER);
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(1);
    expect(ShownValue(USER_ID_TEST_ID)).toBe(TEST_ADMIN_ID);
    expect(ShownValue(LOADING_TEST_ID)).toBe("false");
    expect(setHomeRouteMock).toHaveBeenCalledWith(ADMIN_MENU_ROUTE);
});

it("on the sign-on page the provider's unsent mount read still aborts a page's read", async () => {
    window.history.replaceState(null, "", SIGN_ON_ROUTE);
    const abortedAtSend: (boolean | undefined)[] = [];
    fetchCurrentUserMock.mockImplementation(AnswerUnlessAborted(REGULAR_USER, abortedAtSend));
    render(
        <SessionProvider>
            <RefreshOnMount />
        </SessionProvider>,
    );
    await WaitForSessionLoaded();
    // Only the page's read is sent; the provider's read, which asks no server, replaces it.
    expect(abortedAtSend).toEqual([false]);
    expect(ReadSignal(0).aborted).toBe(true);
    expect(ShownValue(USER_ID_TEST_ID)).toBe(NO_USER_SHOWN);
    expect(setHomeRouteMock).not.toHaveBeenCalled();
});

it.each(MOUNT_FAILURE_CASES)(
    "mounted on $path, $caseName from /auth/me redirects to sign-on $redirects time(s)",
    async ({ path, error, reads, redirects }) => {
        window.history.replaceState(null, "", path);
        fetchCurrentUserMock.mockRejectedValue(error);
        RenderSessionConsumer(CaptureSession);
        await WaitForSessionLoaded();
        expect(fetchCurrentUserMock).toHaveBeenCalledTimes(reads);
        expect(redirectToSignOnMock).toHaveBeenCalledTimes(redirects);
        expect(NavigationAttempts()).toBe(redirects);
        expect(LatestSession().currentUser).toBeNull();
        expect(LatestSession().isLoading).toBe(false);
        expect(setHomeRouteMock).not.toHaveBeenCalled();
    },
);

it.each(ABANDONED_FIRST_READ_CASES)(
    "a first read failing with no answer after $caseName never redirects to sign-on",
    async ({ Abandon, expectedUser, expectedIsLoading, expectedHomeRoute }) => {
        window.history.replaceState(null, "", MAIN_MENU_ROUTE);
        const failFirstRead = DeferFirstReadFailure();
        const view = RenderSessionConsumer(CaptureSession);
        act(() => {
            Abandon(LatestSession(), view);
        });
        expect(ReadSignal(0).aborted).toBe(true);
        await act(async () => {
            failFirstRead(NETWORK_ERROR);
        });
        expect(redirectToSignOnMock).not.toHaveBeenCalled();
        expect(NavigationAttempts()).toBe(0);
        expect(LatestSession().currentUser).toEqual(expectedUser);
        expect(LatestSession().isLoading).toBe(expectedIsLoading);
        expect(sessionStorage.getItem(HOME_ROUTE_KEY)).toBe(expectedHomeRoute);
    },
);

it("StrictMode's effect re-run redirects a first read with no answer to sign-on once", async () => {
    window.history.replaceState(null, "", MAIN_MENU_ROUTE);
    fetchCurrentUserMock.mockRejectedValue(NETWORK_ERROR);
    render(
        <StrictMode>
            <SessionProvider>
                <SessionConsumer />
            </SessionProvider>
        </StrictMode>,
    );
    await WaitForSessionLoaded();
    expect(fetchCurrentUserMock).toHaveBeenCalledTimes(2);
    expect(ReadSignal(0).aborted).toBe(true);
    expect(ReadSignal(1).aborted).toBe(false);
    expect(redirectToSignOnMock).toHaveBeenCalledTimes(1);
    expect(NavigationAttempts()).toBe(1);
    expect(ShownValue(USER_ID_TEST_ID)).toBe(NO_USER_SHOWN);
});

it.each(REFRESH_FAILURE_CASES)(
    "on $path, RefreshSession failing with $caseName gives no user and never redirects",
    async ({ path, error, reads }) => {
        window.history.replaceState(null, "", path);
        fetchCurrentUserMock.mockResolvedValue(REGULAR_USER);
        RenderSessionConsumer(CaptureSession);
        await WaitForSessionLoaded();
        fetchCurrentUserMock.mockRejectedValue(error);
        let refreshedUser: CurrentUserResponse | null = REGULAR_USER;
        await act(async () => {
            refreshedUser = await LatestSession().RefreshSession();
        });
        expect(refreshedUser).toBeNull();
        expect(fetchCurrentUserMock).toHaveBeenCalledTimes(reads);
        expect(redirectToSignOnMock).not.toHaveBeenCalled();
        expect(NavigationAttempts()).toBe(0);
        expect(LatestSession().currentUser).toBeNull();
        expect(LatestSession().isLoading).toBe(false);
    },
);

const ACCOUNT_VIEW_ROUTE = "/accounts/view";
const REPORT_ROUTE = "/reports/transactions";
const ADMIN_USERS_ROUTE = "/admin/users";
const ADMIN_USERS_ADD_ROUTE = "/admin/users/add";
// COMEN01C keeps the trailing space of its literal (cbl/COMEN01C.cbl line 140).
const ADMIN_ONLY_MESSAGE = "No access - Admin Only option... ";

// The page the provider's first read finds the user on, and the menu reads that follow it.
interface MenuTreeCase {
    caseName: string;
    path: string;
    user: CurrentUserResponse;
    mainMenuReads: number;
    adminMenuReads: number;
}

// One way the server answers the page tree's menu read.
interface MenuTreeAnswerCase {
    caseName: string;
    Answer: () => Promise<MenuResponse>;
}

// A page outside the user's own menu tree gets that tree's menu read, which the server's role
// guard refuses with 403. A page in the user's own tree reads no menu, and neither does a tree's
// menu page, which reads its own menu on mount.
const MENU_TREE_CASES: readonly MenuTreeCase[] = [
    {
        caseName: "an admin on CAVW",
        path: ACCOUNT_VIEW_ROUTE,
        user: ADMIN_USER,
        mainMenuReads: 1,
        adminMenuReads: 0,
    },
    {
        caseName: "an admin on CR00",
        path: REPORT_ROUTE,
        user: ADMIN_USER,
        mainMenuReads: 1,
        adminMenuReads: 0,
    },
    {
        caseName: "a regular user on CU01",
        path: ADMIN_USERS_ADD_ROUTE,
        user: REGULAR_USER,
        mainMenuReads: 0,
        adminMenuReads: 1,
    },
    {
        caseName: "a regular user on CU00",
        path: ADMIN_USERS_ROUTE,
        user: REGULAR_USER,
        mainMenuReads: 0,
        adminMenuReads: 1,
    },
    {
        caseName: "a regular user on CAVW",
        path: ACCOUNT_VIEW_ROUTE,
        user: REGULAR_USER,
        mainMenuReads: 0,
        adminMenuReads: 0,
    },
    {
        caseName: "an admin on CU00",
        path: ADMIN_USERS_ROUTE,
        user: ADMIN_USER,
        mainMenuReads: 0,
        adminMenuReads: 0,
    },
    {
        caseName: "an admin on CM00",
        path: MAIN_MENU_ROUTE,
        user: ADMIN_USER,
        mainMenuReads: 0,
        adminMenuReads: 0,
    },
    {
        caseName: "a regular user on CA00",
        path: ADMIN_MENU_ROUTE,
        user: REGULAR_USER,
        mainMenuReads: 0,
        adminMenuReads: 0,
    },
];

// However the server answers, the session shows its user once the menu read settles: the
// interceptor has already routed a refusal, a cancellation needs nothing, and a 200 means the
// server allows the page.
const MENU_TREE_ANSWER_CASES: readonly MenuTreeAnswerCase[] = [
    { caseName: "a 403", Answer: () => Promise.reject(BuildResponseError(HTTP_FORBIDDEN)) },
    { caseName: "a cancellation", Answer: () => Promise.reject(new CanceledError()) },
    { caseName: "a 200", Answer: () => Promise.resolve({ options: [] }) },
];

// RequireAdmin's refusal of the admin menu read, as the apiClient interceptor meets it.
function BuildAdminMenuRefusal(): AxiosError {
    const config: InternalAxiosRequestConfig = { url: "/menu/admin", headers: new AxiosHeaders() };
    const data = { message: ADMIN_ONLY_MESSAGE, field_errors: [], cursor_field: null };
    const response = { status: HTTP_FORBIDDEN, data, statusText: "", headers: {}, config };
    return new AxiosError(
        "Request failed",
        AxiosError.ERR_BAD_REQUEST,
        config,
        undefined,
        response,
    );
}

// The interceptor's bare /auth/me lookup of the refused user's own menu.
function AnswerHomeRouteLookup(user: CurrentUserResponse): jest.SpyInstance {
    const config = BuildCurrentUserConfig();
    const answer: AxiosResponse = { data: user, status: 200, statusText: "", headers: {}, config };
    return jest.spyOn(axios, "get").mockResolvedValue(answer);
}

it.each(MENU_TREE_CASES)(
    "$caseName reads the main menu $mainMenuReads and the admin menu $adminMenuReads time(s)",
    async ({ path, user, mainMenuReads, adminMenuReads }) => {
        window.history.replaceState(null, "", path);
        await RenderSignedOnSession(user);
        expect(fetchMainMenuMock).toHaveBeenCalledTimes(mainMenuReads);
        expect(fetchAdminMenuMock).toHaveBeenCalledTimes(adminMenuReads);
        const menuReads = [...fetchMainMenuMock.mock.calls, ...fetchAdminMenuMock.mock.calls];
        expect(menuReads.every(([options]) => options?.signal === ReadSignal(0))).toBe(true);
        expect(LatestSession().currentUser).toEqual(user);
    },
);

it.each(MENU_TREE_ANSWER_CASES)(
    "an admin on CAVW is shown once the main menu read settles with $caseName",
    async ({ Answer }) => {
        window.history.replaceState(null, "", ACCOUNT_VIEW_ROUTE);
        fetchMainMenuMock.mockImplementationOnce(Answer);
        await RenderSignedOnSession(ADMIN_USER);
        expect(fetchMainMenuMock).toHaveBeenCalledTimes(1);
        expect(LatestSession().currentUser).toEqual(ADMIN_USER);
        expect(LatestSession().isLoading).toBe(false);
        expect(redirectToSignOnMock).not.toHaveBeenCalled();
    },
);

it("a regular user on CU01 returns to /menu with the server's admin-only literal", async () => {
    window.history.replaceState(null, "", ADMIN_USERS_ADD_ROUTE);
    fetchAdminMenuMock.mockImplementationOnce(() => HandleResponseError(BuildAdminMenuRefusal()));
    const lookupSpy = AnswerHomeRouteLookup(REGULAR_USER);
    await RenderSignedOnSession(REGULAR_USER);
    expect(fetchAdminMenuMock).toHaveBeenCalledTimes(1);
    expect(lookupSpy).toHaveBeenCalledTimes(1);
    expect(NavigationAttempts()).toBe(1);
    expect(sessionStorage.getItem(PENDING_MESSAGE_KEY)).toBe(ADMIN_ONLY_MESSAGE);
    // The message is kept for the main menu, so CU01, mounting while the browser leaves it, cannot
    // take it.
    expect(sessionStorage.getItem(PENDING_MESSAGE_ROUTE_KEY)).toBe(MAIN_MENU_ROUTE);
    lookupSpy.mockRestore();
});

// Only the provider's first read checks the page's tree: RefreshSession's read never does, on the
// sign-on page after a login or on any other page.
it.each([SIGN_ON_ROUTE, ACCOUNT_VIEW_ROUTE])(
    "on %s, a RefreshSession answering an admin reads no menu",
    async (path) => {
        window.history.replaceState(null, "", path);
        await RenderSignedOnSession(REGULAR_USER);
        fetchCurrentUserMock.mockResolvedValue(ADMIN_USER);
        await act(async () => {
            await LatestSession().RefreshSession();
        });
        expect(LatestSession().currentUser).toEqual(ADMIN_USER);
        expect(fetchMainMenuMock).not.toHaveBeenCalled();
        expect(fetchAdminMenuMock).not.toHaveBeenCalled();
    },
);

it.each(ABANDONED_FIRST_READ_CASES)(
    "an admin's first read on CAVW answering after $caseName reads no menu",
    async ({ Abandon }) => {
        window.history.replaceState(null, "", ACCOUNT_VIEW_ROUTE);
        const resolveUser = DeferCurrentUser();
        const view = RenderSessionConsumer(CaptureSession);
        act(() => {
            Abandon(LatestSession(), view);
        });
        expect(ReadSignal(0).aborted).toBe(true);
        await act(async () => {
            resolveUser(ADMIN_USER);
        });
        expect(fetchMainMenuMock).not.toHaveBeenCalled();
        expect(fetchAdminMenuMock).not.toHaveBeenCalled();
    },
);
